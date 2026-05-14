"""
fetch_scopus_journals.py
========================
Fetches all Scopus-indexed serial titles with ISSN, name, CiteScore,
percentile, and quartile via the Elsevier Serial Title API.

Strategy
--------
The Serial Title API has no "dump all" endpoint. We iterate over all 27
Scopus top-level ASJC subject-area abbreviations, paginate in steps of 200,
and deduplicate on Scopus source-id.

The CITESCORE view returns citeScoreYearInfoList with per-year CiteScore and
per-subject-category percentile. For each journal we use the most recent
completed year and the best-performing subject category (highest percentile),
matching Scopus's default display.

Quartile is derived from percentile:
  Q1 >= 75th, Q2 >= 50th, Q3 >= 25th, Q4 < 25th

Usage
-----
    export SCOPUS_API_KEY="your_key_here"
    python fetch_scopus_journals.py

Output
------
    scopus_journals.csv
"""

import os
import sys
import time
import logging
import requests
import pandas as pd
from urllib.parse import urlparse, parse_qs
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY     = os.environ.get("SCOPUS_API_KEY", "")
BASE_URL    = "https://api.elsevier.com/content/serial/title"
OUTPUT_FILE = "scopus_journals.csv"
PAGE_SIZE   = 200
REQUEST_DELAY = 0.4  # seconds; increase to 1.0 if you hit 429s

SUBJECT_AREAS = [
    "AGRI",  # Agricultural and Biological Sciences
    "BIOC",  # Biochemistry, Genetics and Molecular Biology
    "CENG",  # Chemical Engineering (incl. biomedical engineering)
    "CHEM",  # Chemistry
    "DENT",  # Dentistry
    "ENVI",  # Environmental Science (incl. environmental health)
    "HEAL",  # Health Professions
    "IMMU",  # Immunology and Microbiology
    "MATE",  # Materials Science (incl. biomaterials)
    "MEDI",  # Medicine
    "NEUR",  # Neuroscience
    "NURS",  # Nursing
    "PHAR",  # Pharmacology, Toxicology and Pharmaceutics
    "PSYC",  # Psychology
    "VETE",  # Veterinary
    "MULT",  # Multidisciplinary
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "X-ELS-APIKey": API_KEY,
    "Accept": "application/json",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def quartile_from_percentile(pct):
    if pct is None:
        return None
    pct = float(pct)
    if pct >= 75: return 1
    if pct >= 50: return 2
    if pct >= 25: return 3
    return 4


def total_from_links(links: list):
    """
    The API does not return a totalResults field in the CITESCORE view.
    The 'last' pagination link encodes the final start index.
    total = last_start + count.
    The API caps display at start=9999, so for large subject areas the
    true total may exceed 10,000. We paginate until fewer entries than
    requested are returned rather than relying on this estimate.
    """
    for lnk in links:
        if lnk.get("@ref") == "last":
            qs = parse_qs(urlparse(lnk["@href"]).query)
            last_start = int(qs.get("start", [0])[0])
            count      = int(qs.get("count", [PAGE_SIZE])[0])
            return last_start + count
    return None


def parse_entry(entry: dict) -> dict:
    """
    Extract fields from one serial-title API entry.

    CiteScore / percentile / quartile come from the most recent completed
    year in citeScoreYearInfoList. The best subject-category rank (highest
    percentile) is selected, consistent with the Scopus website default.
    """
    source_id = entry.get("source-id", "")
    title     = entry.get("dc:title", "")
    issn      = entry.get("prism:issn", "")
    eissn     = entry.get("prism:eIssn", "")

    csyl      = entry.get("citeScoreYearInfoList", {})
    citescore = csyl.get("citeScoreCurrentMetric")
    cs_year   = csyl.get("citeScoreCurrentMetricYear")

    best_pct  = None
    best_subj = None

    year_infos = csyl.get("citeScoreYearInfo", [])
    if isinstance(year_infos, dict):
        year_infos = [year_infos]

    for yi in year_infos:
        if yi.get("@year") != cs_year:
            continue
        if yi.get("@status") != "Complete":
            continue
        cs_info_lists = yi.get("citeScoreInformationList", [])
        if isinstance(cs_info_lists, dict):
            cs_info_lists = [cs_info_lists]
        for ci_block in cs_info_lists:
            ci_list = ci_block.get("citeScoreInfo", [])
            if isinstance(ci_list, dict):
                ci_list = [ci_list]
            for ci in ci_list:
                subj_ranks = ci.get("citeScoreSubjectRank", [])
                if isinstance(subj_ranks, dict):
                    subj_ranks = [subj_ranks]
                for sr in subj_ranks:
                    try:
                        pct = float(sr.get("percentile", -1))
                    except (TypeError, ValueError):
                        pct = -1
                    if pct > (best_pct if best_pct is not None else -1):
                        best_pct  = pct
                        best_subj = sr.get("subjectCode")

    return {
        "source_id":         source_id,
        "title":             title,
        "issn":              issn,
        "eissn":             eissn,
        "citescore":         citescore,
        "citescore_year":    cs_year,
        "percentile":        best_pct,
        "quartile":          quartile_from_percentile(best_pct),
        "best_subject_code": best_subj,
    }


def fetch_page(subj: str, start: int, count: int = PAGE_SIZE) -> dict:
    params = {"subj": subj, "view": "CITESCORE", "count": count, "start": start}
    for attempt in range(5):
        try:
            resp = SESSION.get(BASE_URL, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning("Rate limited (429). Waiting %ds ...", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                log.error("Unauthorised (401). Check API key / IP entitlement.")
                sys.exit(1)
            log.warning(
                "HTTP %d for subj=%s start=%d (attempt %d)",
                resp.status_code, subj, start, attempt + 1,
            )
            time.sleep(2)
        except requests.RequestException as exc:
            log.warning("Request error: %s (attempt %d)", exc, attempt + 1)
            time.sleep(3)
    log.error("Failed to fetch subj=%s start=%d after 5 attempts.", subj, start)
    return {}

# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def harvest_all() -> pd.DataFrame:
    if not API_KEY:
        log.error("Set SCOPUS_API_KEY environment variable.")
        sys.exit(1)

    all_records:     list = []
    seen_source_ids: set  = set()

    for subj in tqdm(SUBJECT_AREAS, desc="Subject areas", unit="subj"):
        start = 0
        total = None

        while True:
            data = fetch_page(subj, start)
            time.sleep(REQUEST_DELAY)

            if not data:
                break

            container = data.get("serial-metadata-response", {})

            if total is None:
                links = container.get("link", [])
                total = total_from_links(links)
                log.info("Subject %-6s estimated total: %s", subj, total)

            entries = container.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            if not entries:
                break

            for entry in entries:
                sid = str(entry.get("source-id", ""))
                if sid in seen_source_ids:
                    continue
                seen_source_ids.add(sid)
                all_records.append(parse_entry(entry))

            # Stop when last page (fewer entries than requested).
            if len(entries) < PAGE_SIZE:
                break

            start += PAGE_SIZE

    log.info("Total unique records: %d", len(all_records))
    return pd.DataFrame(all_records)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Scopus journal harvest ...")
    df = harvest_all()
    df = df.dropna(subset=["citescore_year"]).sort_values("title", ignore_index=True)
    df.to_csv(OUTPUT_FILE, index=False)
    log.info("Saved %d records to %s", len(df), OUTPUT_FILE)
    with_cs = df["citescore"].notna().sum()
    log.info(
        "Records with CiteScore: %d / %d (%.1f%%)",
        with_cs, len(df), 100 * with_cs / max(len(df), 1),
    )
    print(df.head(10).to_string(index=False))

