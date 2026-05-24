"""
fetch_scopus_journals.py
========================
Fetches all Scopus-indexed serial titles via the Elsevier Serial Title API
and writes everything the API returns to a CSV, with derived CiteScore metrics
appended as extra columns.

Strategy
--------
The Serial Title API has no "dump all" endpoint. We iterate over all
PubMed-relevant ASJC subject-area abbreviations, paginate in steps of 200,
and deduplicate on Scopus source-id. All fields from the API response are
retained; namespace prefixes (dc:, prism:, @) are stripped from column names.
Nested objects are JSON-serialised to strings to keep the CSV flat.
Derived columns appended: citescore, citescore_year, percentile, quartile,
best_subject_code.

Quartile derivation (consistent with Scopus):
  Q1: percentile >= 75  (top 25%)
  Q2: percentile >= 50
  Q3: percentile >= 25
  Q4: percentile <  25  (bottom 25%)

Rows without a citescore_year are excluded from the output.

Usage
-----
    python fetch_scopus_journals.py
    (reads elsevier_api_key from config.yaml)

Output
------
    data/scopus_journals.csv
"""

import csv
import json
import sys
import time
import logging
import requests
import yaml
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_cfg_path = Path(__file__).parent / "config.yaml"
try:
    with open(_cfg_path) as _f:
        _cfg    = yaml.safe_load(_f) or {}
    API_KEY = _cfg.get("elsevier_api_key", "")
    DB_PATH = str(Path(__file__).parent / _cfg.get("db_path", "data/paperdigest.db"))
except Exception:
    API_KEY = ""
    DB_PATH = str(Path(__file__).parent / "data/paperdigest.db")

BASE_URL      = "https://api.elsevier.com/content/serial/title"
OUTPUT_FILE   = Path(__file__).parent / "data" / "scopus_journals.csv"
STATUS_FILE   = Path(__file__).parent / "data" / "scopus_refresh_status.json"
PAGE_SIZE     = 200
REQUEST_DELAY = 0.4  # seconds; increase to 1.0 if you hit 429s

# The Elsevier Serial Title API caps pagination at offset 10,000 per subj query.
# All 4-letter codes are used as-is EXCEPT "MEDI" (Medicine), which alone has
# more than 10,000 journals and silently drops the tail (e.g. The Lancet
# Infectious Diseases). MEDI is replaced with its 40 granular 4-digit ASJC
# sub-codes (2700–2739), each covering one specialty (e.g. 2725 = Infectious
# Diseases) with far fewer journals. Journals appear under multiple sub-codes;
# dedup by source-id handles overlap. The 4-letter codes for non-MEDI areas are
# kept because the API rejects 4-digit codes for non-biomedical domains
# (e.g. 15xx/Chemical Engineering → HTTP 400).
SUBJECT_AREAS = [
    "AGRI",  # Agricultural and Biological Sciences
    "BIOC",  # Biochemistry, Genetics and Molecular Biology
    "CENG",  # Chemical Engineering
    "CHEM",  # Chemistry
    "DENT",  # Dentistry
    "ENVI",  # Environmental Science
    "HEAL",  # Health Professions
    "IMMU",  # Immunology and Microbiology
    "MATE",  # Materials Science
    # "MEDI" alone is capped at 10,000 by the API — the broad code is kept to
    # preserve its arbitrary first-10,000 result set, while the 40 granular
    # 27xx sub-codes are added alongside it to capture the tail that MEDI cuts
    # off. Dedup by source-id handles the overlap between the two result sets.
    "MEDI",
    "2700", "2701", "2702", "2703", "2704", "2705", "2706", "2707",
    "2708", "2709", "2710", "2711", "2712", "2713", "2714", "2715",
    "2716", "2717", "2718", "2719", "2720", "2721", "2722", "2723",
    "2724", "2725", "2726", "2727", "2728", "2729", "2730", "2731",
    "2732", "2733", "2734", "2735", "2736", "2737", "2738", "2739",
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
SESSION.headers.update({"Accept": "application/json"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_status(data: dict):
    """Atomically write refresh status JSON so the reader never sees partial data."""
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(STATUS_FILE)


def quartile_from_percentile(pct):
    if pct is None:
        return None
    pct = float(pct)
    if pct >= 75: return 1
    if pct >= 50: return 2
    if pct >= 25: return 3
    return 4


def total_from_links(links: list):
    for lnk in links:
        if lnk.get("@ref") == "last":
            qs = parse_qs(urlparse(lnk["@href"]).query)
            last_start = int(qs.get("start", [0])[0])
            count      = int(qs.get("count", [PAGE_SIZE])[0])
            return last_start + count
    return None


def strip_prefix(key: str) -> str:
    """Strip namespace prefixes (dc:, prism:, @) from keys."""
    if ":" in key:
        key = key.split(":", 1)[1]
    return key.lstrip("@")


def flatten_entry(entry: dict) -> dict:
    """Shallow-flatten entry dict, stripping prefixes; serialise nested values."""
    flat = {}
    for k, v in entry.items():
        clean_key = strip_prefix(k)
        if isinstance(v, (dict, list)):
            flat[clean_key] = json.dumps(v, ensure_ascii=False)
        else:
            flat[clean_key] = v
    return flat


def parse_entry(entry: dict) -> dict:
    """
    Return the full flattened API entry plus derived CiteScore metrics.
    Uses the most recent completed year and best-performing subject category.
    """
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

    row = flatten_entry(entry)
    row["citescore"]         = citescore
    row["citescore_year"]    = cs_year
    row["percentile"]        = best_pct
    row["quartile"]          = quartile_from_percentile(best_pct)
    row["best_subject_code"] = best_subj
    return row


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
            if resp.status_code == 400:
                log.warning("Bad request (400) for subj=%s — code not accepted by API; skipping.", subj)
                return {}
            log.warning(
                "HTTP %d for subj=%s start=%d (attempt %d)",
                resp.status_code, subj, start, attempt + 1,
            )
            time.sleep(2)
        except requests.RequestException as exc:
            log.warning("Request error: %s (attempt %d)", exc, attempt + 1)
            time.sleep(3)
    log.warning("Could not fetch subj=%s start=%d after 5 attempts.", subj, start)
    return {}

# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

def fetch_all() -> list:
    if not API_KEY:
        log.error("Set elsevier_api_key in config.yaml.")
        _write_status({"status": "error", "message": "elsevier_api_key not set in config.yaml"})
        sys.exit(1)

    SESSION.headers.update({"X-ELS-APIKey": API_KEY})

    all_records:     list = []
    seen_source_ids: set  = set()
    n_subjects = len(SUBJECT_AREAS)

    # Phase 1: fetch the first page of every subject to discover per-subject
    # totals. These first-page records are kept; the totals are summed into
    # grand_total so the progress bar has an accurate denominator from the start
    # of phase 2.
    _write_status({"status": "running", "phase": "discovering",
                   "done": 0, "total": n_subjects})

    first_pages: dict = {}  # subj -> (entries, subj_total | None)
    for idx, subj in enumerate(SUBJECT_AREAS):
        log.info("Discovering %d/%d: %s", idx + 1, n_subjects, subj)
        data = fetch_page(subj, 0)
        time.sleep(REQUEST_DELAY)
        if data:
            container  = data.get("serial-metadata-response", {})
            links      = container.get("link", [])
            subj_total = total_from_links(links)
            entries    = container.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            if subj_total is None and entries:
                subj_total = len(entries)  # single-page subject
        else:
            subj_total = None
            entries    = []
        first_pages[subj] = (entries, subj_total)
        if subj_total is not None and subj_total >= 10000:
            log.warning(
                "  WARN: subj=%s reports %d results — API pagination cap reached; "
                "journals beyond 10,000 will be missed. Sub-divide this code further.",
                subj, subj_total,
            )
        log.info("  estimated total: %s", subj_total)
        _write_status({"status": "running", "phase": "discovering",
                       "done": idx + 1, "total": n_subjects})

    grand_total = sum(t for _, t in first_pages.values() if t is not None)
    log.info("Grand total across all subject areas: %d", grand_total)

    # Phase 2: process first-page records then continue pagination for each
    # subject. Stop when start >= subj_total to avoid the Elsevier API's hard
    # pagination cap (offset 10 000 returns HTTP 500 for large subjects).
    fetched_raw = 0
    _write_status({"status": "running", "phase": "fetching",
                   "fetched": 0, "total": grand_total, "subject": ""})

    for idx, subj in enumerate(SUBJECT_AREAS):
        entries, subj_total = first_pages[subj]
        log.info("Subject %d/%d: %s (total: %s)", idx + 1, n_subjects, subj, subj_total)

        for entry in entries:
            fetched_raw += 1
            sid = str(entry.get("source-id", ""))
            if sid not in seen_source_ids:
                seen_source_ids.add(sid)
                all_records.append(parse_entry(entry))

        start = PAGE_SIZE
        while True:
            if subj_total is not None and start >= subj_total:
                break

            data = fetch_page(subj, start)
            time.sleep(REQUEST_DELAY)
            if not data:
                break

            container = data.get("serial-metadata-response", {})
            page_entries = container.get("entry", [])
            if isinstance(page_entries, dict):
                page_entries = [page_entries]
            if not page_entries:
                break

            for entry in page_entries:
                fetched_raw += 1
                sid = str(entry.get("source-id", ""))
                if sid not in seen_source_ids:
                    seen_source_ids.add(sid)
                    all_records.append(parse_entry(entry))

            if len(page_entries) < PAGE_SIZE:
                break

            start += PAGE_SIZE
            _write_status({"status": "running", "phase": "fetching",
                           "fetched": fetched_raw, "total": grand_total,
                           "subject": subj})

        _write_status({"status": "running", "phase": "fetching",
                       "fetched": fetched_raw, "total": grand_total,
                       "subject": subj})

    log.info("Total unique records: %d", len(all_records))
    return all_records

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _recategorise_papers(db_path: str, n_journals: int) -> int:
    """Update all papers in the DB with quartile/CiteScore/percentile from the freshly written CSV."""
    import sqlite3 as _sqlite3

    mapping: dict = {}
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                q_num    = int(float(row.get("quartile") or ""))
                quartile = f"Q{q_num}" if 1 <= q_num <= 4 else None
            except (ValueError, TypeError):
                quartile = None
            if quartile is None:
                continue
            try:
                citescore: float | None = float(row.get("citescore") or "")
            except (ValueError, TypeError):
                citescore = None
            try:
                percentile: float | None = float(row.get("percentile") or "")
            except (ValueError, TypeError):
                percentile = None
            publisher = (row.get("publisher") or "").strip() or None
            for field in (row.get("issn") or "", row.get("eIssn") or ""):
                for raw in field.split(","):
                    norm = raw.replace("-", "").strip().upper()
                    if norm and norm not in mapping:
                        mapping[norm] = (quartile, citescore, percentile, publisher)

    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    _write_status({"status": "running", "phase": "recategorising", "records": n_journals})
    try:
        papers  = conn.execute(
            "SELECT pmid, issn FROM papers WHERE issn IS NOT NULL"
        ).fetchall()
        updated = 0
        for paper in papers:
            norm = paper["issn"].replace("-", "").strip().upper()
            data = mapping.get(norm)
            if data:
                q, cs, pct, pub = data
                conn.execute(
                    """UPDATE papers
                       SET scopus_quartile   = ?,
                           scopus_citescore  = ?,
                           scopus_percentile = ?,
                           publisher = CASE WHEN ? IS NOT NULL THEN ? ELSE publisher END
                       WHERE pmid = ?""",
                    (q, cs, pct, pub, pub, paper["pmid"]),
                )
                updated += 1
            else:
                conn.execute(
                    """UPDATE papers
                       SET scopus_quartile = NULL, scopus_citescore = NULL,
                           scopus_percentile = NULL WHERE pmid = ?""",
                    (paper["pmid"],),
                )
        conn.commit()
        log.info("Re-applied rankings to %d papers.", updated)
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def drop_json_columns(records: list) -> list:
    """Drop any column whose first non-null string value parses as a JSON object or array."""
    if not records:
        return records
    all_keys = {k for r in records for k in r}
    json_cols = set()
    for key in all_keys:
        for r in records:
            val = r.get(key)
            if val is None or not isinstance(val, str):
                continue
            try:
                parsed = json.loads(val)
                if isinstance(parsed, (dict, list)):
                    json_cols.add(key)
            except (ValueError, TypeError):
                pass
            break
    if json_cols:
        log.info("Dropping JSON-serialised columns: %s", sorted(json_cols))
    return [{k: v for k, v in r.items() if k not in json_cols} for r in records]


if __name__ == "__main__":
    log.info("Starting Scopus journal data refresh ...")
    try:
        records = fetch_all()
        records = [r for r in records if r.get("citescore_year") is not None]
        records = sorted(records, key=lambda r: (r.get("title") or "").casefold())
        records = drop_json_columns(records)

        OUTPUT_FILE.parent.mkdir(exist_ok=True)
        if records:
            # Collect the union of all keys across all records (some records
            # have extra fields like 'issn' and 'url' that are absent in others).
            seen_keys: set = set()
            fieldnames: list = []
            for r in records:
                for k in r:
                    if k not in seen_keys:
                        seen_keys.add(k)
                        fieldnames.append(k)
            with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
                writer.writeheader()
                writer.writerows(records)

        n = len(records)
        with_cs = sum(1 for r in records if r.get("citescore") is not None)
        log.info("Saved %d records to %s", n, OUTPUT_FILE)
        log.info(
            "Records with CiteScore: %d / %d (%.1f%%)",
            with_cs, n, 100 * with_cs / max(n, 1),
        )

        log.info("Re-applying rankings to existing papers ...")
        _recategorise_papers(DB_PATH, n)

        _write_status({"status": "done", "records": n})
    except Exception:
        _write_status({"status": "error"})
        raise
    finally:
        # Always reset to idle so a page reload doesn't trigger an infinite
        # reload loop even if the script crashed part-way through.
        if STATUS_FILE.exists():
            try:
                current = json.loads(STATUS_FILE.read_text())
                if current.get("status") not in ("error",):
                    _write_status({"status": "idle"})
            except Exception:
                _write_status({"status": "idle"})
