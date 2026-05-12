#!/usr/bin/env python3
"""
Standalone cron script: fetch new papers from PubMed, filter by Scopus
quartile, and store them in the SQLite database.

Invoked directly; does NOT import from app/.
"""
import csv
import html as _html
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
import yaml

BASE_DIR = Path(__file__).parent


# ── Config loading ──────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f) or {}


def load_user_configs(config: dict) -> list[tuple[str, dict]]:
    users_dir = BASE_DIR / "users"
    result = []
    if not users_dir.exists():
        return result
    for path in sorted(users_dir.glob("*.yaml")):
        username = path.stem
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if data.get("fetch_enabled", True):
            result.append((username, data))
    return result


def should_fetch_today(user_cfg: dict) -> bool:
    schedule = user_cfg.get("fetch_schedule", "daily")
    if schedule == "daily":
        return True
    today = datetime.now(timezone.utc).date()
    if schedule == "weekly":
        dow = int(user_cfg.get("fetch_schedule_dow", 0))
        return today.weekday() == dow
    if schedule == "monthly":
        dom = max(1, min(28, int(user_cfg.get("fetch_schedule_dom", 1))))
        return today.day == dom
    return True


# ── Database ─────────────────────────────────────────────────────────────────

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def conn_ctx(db_path: str):
    conn = get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Scopus loading ────────────────────────────────────────────────────────────

def _norm_issn(issn: str) -> str:
    return issn.replace("-", "").strip().upper()


def _parse_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def load_scopus(scopus_path: str) -> dict[str, tuple[str, float | None, float | None, float | None, str | None, int | None]]:
    """Returns ISSN (hyphen-free) -> (quartile, cites_2yr, cites_3yr, sjr, publisher, h_index)."""
    mapping: dict[str, tuple[str, float | None, float | None, float | None, str | None, int | None]] = {}
    p = Path(scopus_path)
    if not p.exists():
        print(f"[fetch] WARNING: Scopus file not found at {scopus_path}. Quartile filtering disabled.")
        return mapping
    with open(p, encoding="utf-8-sig") as f:
        # SCImago uses semicolon delimiter; Publisher appears twice — DictReader keeps last value
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            quartile = (row.get("SJR Best Quartile") or "").strip()
            if quartile not in ("Q1", "Q2", "Q3", "Q4"):
                continue
            cites_2yr = _parse_float(row.get("Citations / Doc. (2years)") or "")
            # col27 ("Citations / Doc. (3years)") is unreliable when Categories (col25)
            # contains unquoted semicolons — fall back to col15/col13 when not parseable.
            cites_3yr = _parse_float(row.get("Citations / Doc. (3years)") or "")
            if cites_3yr is None:
                tc = _parse_float(row.get("Total Citations (3years)") or "")
                td = _parse_float(row.get("Total Docs. (3years)") or "")
                if tc is not None and td:
                    cites_3yr = tc / td
            sjr = _parse_float(row.get("SJR") or "")
            publisher = (row.get("Publisher") or "").strip() or None
            try:
                h_index: int | None = int(row.get("H index") or "")
            except (ValueError, TypeError):
                h_index = None
            issn_field = row.get("Issn") or row.get("ISSN") or ""
            for raw_issn in issn_field.split(","):
                raw_issn = raw_issn.strip()
                if raw_issn:
                    norm = _norm_issn(raw_issn)
                    if norm not in mapping:
                        mapping[norm] = (quartile, cites_2yr, cites_3yr, sjr, publisher, h_index)
    return mapping


# ── PubMed fetching ───────────────────────────────────────────────────────────

def _date_range(lookback_days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    return str(start).replace("-", "/"), str(today).replace("-", "/")


def search_pubmed(query: str, mindate: str, maxdate: str,
                  api_key: str, retmax: int = 200) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "mindate": mindate,
        "maxdate": maxdate,
        "datetype": "edat",
        "retmax": retmax,
        "retmode": "json",
        "api_key": api_key,
    }
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_pubmed_records(pmids: list[str], api_key: str) -> ET.Element:
    ids = ",".join(pmids)
    params = {
        "db": "pubmed",
        "id": ids,
        "rettype": "xml",
        "retmode": "xml",
        "api_key": api_key,
    }
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return ET.fromstring(r.content)


def _text(el, path: str, default: str = "") -> str:
    node = el.find(path)
    return node.text.strip() if node is not None and node.text else default


def _html_text(el) -> str:
    """Return element text with <i> children converted to <em>, all text HTML-escaped."""
    if el is None:
        return ""
    parts = []
    if el.text:
        parts.append(_html.escape(el.text))
    for child in el:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        inner = _html.escape("".join(child.itertext()))
        parts.append(f"<em>{inner}</em>" if tag == "i" else inner)
        if child.tail:
            parts.append(_html.escape(child.tail))
    return "".join(parts).strip()


def parse_article(article_set_child: ET.Element) -> dict | None:
    """Parse a single PubmedArticle XML element into a dict."""
    medline = article_set_child.find(".//MedlineCitation")
    if medline is None:
        return None

    pmid_el = medline.find("PMID")
    if pmid_el is None or not pmid_el.text:
        return None
    pmid = pmid_el.text.strip()

    art = medline.find("Article")
    if art is None:
        return None

    title = _html_text(art.find("ArticleTitle"))
    abstract_parts = [
        "".join(node.itertext()).strip()
        for node in art.findall(".//AbstractText")
    ]
    abstract = " ".join(p for p in abstract_parts if p)

    # Authors
    authors = []
    for auth in art.findall(".//Author"):
        ln = _text(auth, "LastName")
        fn = _text(auth, "ForeName") or _text(auth, "Initials")
        if ln:
            authors.append(f"{ln}, {fn}".strip(", "))
        else:
            cn = _text(auth, "CollectiveName")
            if cn:
                authors.append(cn)

    # Journal
    journal_el = art.find("Journal")
    journal_name = ""
    issn = ""
    if journal_el is not None:
        journal_name = _text(journal_el, "Title") or _text(journal_el, "ISOAbbreviation")
        # prefer print ISSN, fall back to electronic
        issn_el = journal_el.find("ISSN[@IssnType='Print']")
        if issn_el is None:
            issn_el = journal_el.find("ISSN")
        if issn_el is not None and issn_el.text:
            issn = issn_el.text.strip()

    # Publication date (journal issue date, may be in the future)
    pub_date = ""
    pub_date_el = art.find(".//PubDate")
    if pub_date_el is not None:
        year = _text(pub_date_el, "Year")
        month = _text(pub_date_el, "Month")
        day = _text(pub_date_el, "Day")
        med_date = _text(pub_date_el, "MedlineDate")
        if year:
            pub_date = f"{year}-{month[:3] if month else ''}-{day}".strip("-")
        elif med_date:
            pub_date = med_date

    # Epub date (electronic publication, usually earlier than print)
    epub_date = ""
    for date_el in art.findall(".//ArticleDate"):
        if date_el.get("DateType") == "Electronic":
            y = _text(date_el, "Year")
            m = _text(date_el, "Month")
            d = _text(date_el, "Day")
            if y and m and d:
                epub_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            break

    # DOI
    doi = ""
    for id_el in article_set_child.findall(".//ArticleId"):
        if id_el.get("IdType") == "doi":
            doi = (id_el.text or "").strip()
            break

    # MeSH terms
    mesh_terms = [
        node.text.strip()
        for node in medline.findall(".//MeshHeading/DescriptorName")
        if node.text
    ]

    return {
        "pmid": pmid,
        "title": title,
        "authors": json.dumps(authors),
        "journal": journal_name,
        "issn": issn,
        "pub_date": pub_date,
        "epub_date": epub_date,
        "abstract": abstract,
        "doi": doi,
        "mesh_terms": json.dumps(mesh_terms),
    }


# ── Unpaywall ─────────────────────────────────────────────────────────────────

def get_oa_url(doi: str, email: str) -> str | None:
    if not doi:
        return None
    try:
        url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            loc = data.get("best_oa_location")
            if loc:
                return loc.get("url_for_pdf") or loc.get("url")
    except Exception:
        pass
    return None


# ── Email sending ─────────────────────────────────────────────────────────────

def _log_mail(db_path: str, user_id: int, to: str, subject: str,
              status: str, error: str = None):
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with conn_ctx(db_path) as conn:
            conn.execute(
                """INSERT INTO mail_log (user_id, sent_at, to_addr, subject, status, error)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, now_iso, to, subject, status, error),
            )
    except Exception as e:
        print(f"[fetch] Could not write mail_log: {e}", file=sys.stderr)


def send_error_email(config: dict, db_path: str, user_id: int,
                     to: str, subject: str, body: str):
    from mail_helper import send_plain_email
    app_name = config.get("app_name", "Papers Daily")
    full_subject = f"[{app_name}] {subject}"
    try:
        send_plain_email(config, to, full_subject, body)
        _log_mail(db_path, user_id, to, full_subject, "sent")
    except Exception as e:
        print(f"[fetch] Failed to send error email: {e}", file=sys.stderr)
        _log_mail(db_path, user_id, to, full_subject, "error", str(e))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    api_key = config.get("ncbi_api_key", "")
    ncbi_email = config.get("ncbi_email", "anonymous@example.com")
    db_path = str(BASE_DIR / config.get("db_path", "data/paperdigest.db"))
    scopus_path = str(BASE_DIR / config.get("scopus_file", "data/scopus.csv"))
    scopus = load_scopus(scopus_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    users = load_user_configs(config)
    if not users:
        print("[fetch] No users with fetch_enabled found.")
        return

    # Ensure DB tables exist
    from app import db as appdb
    appdb.init_db(db_path)

    # Backfill scopus fields for papers stored before these fields were tracked
    if scopus:
        with conn_ctx(db_path) as conn:
            to_fix = conn.execute(
                """SELECT pmid, issn FROM papers
                   WHERE (scopus_cites_per_doc IS NULL OR publisher IS NULL
                          OR scopus_cites_3yr IS NULL OR scopus_h_index IS NULL)
                     AND issn IS NOT NULL"""
            ).fetchall()
            updated = 0
            for row in to_fix:
                data = scopus.get(_norm_issn(row["issn"]))
                if data:
                    _, cites_2yr, cites_3yr, _, pub, h_index = data
                    conn.execute(
                        """UPDATE papers
                           SET scopus_cites_per_doc = COALESCE(scopus_cites_per_doc, ?),
                               scopus_cites_3yr = COALESCE(scopus_cites_3yr, ?),
                               scopus_h_index = COALESCE(scopus_h_index, ?),
                               publisher = COALESCE(publisher, ?)
                           WHERE pmid = ?""",
                        (cites_2yr, cites_3yr, h_index, pub, row["pmid"]),
                    )
                    updated += 1
        if updated:
            print(f"[fetch] Backfilled Scopus fields for {updated} papers")

    # Backfill titles that were truncated by old XML parsing (text before first <i> child only)
    with conn_ctx(db_path) as conn:
        short_pmids = [
            r["pmid"] for r in conn.execute(
                "SELECT pmid FROM papers WHERE length(trim(title)) < 30"
            ).fetchall()
        ]
    if short_pmids:
        print(f"[fetch] Re-fetching titles for {len(short_pmids)} potentially truncated papers")
        try:
            xml_root = fetch_pubmed_records(short_pmids, api_key)
            with conn_ctx(db_path) as conn:
                for article_el in xml_root.findall(".//PubmedArticle"):
                    rec = parse_article(article_el)
                    if rec:
                        conn.execute(
                            "UPDATE papers SET title = ? WHERE pmid = ?",
                            (rec["title"], rec["pmid"]),
                        )
        except Exception as e:
            print(f"[fetch] Title backfill failed: {e}", file=sys.stderr)

    for username, user_cfg in users:
        if not should_fetch_today(user_cfg):
            schedule = user_cfg.get("fetch_schedule", "daily")
            print(f"[fetch] {username}: skipping (schedule={schedule})")
            continue

        user_email = user_cfg.get("email", "")
        q2_hard = user_cfg.get("q2_hard", True)
        profiles = user_cfg.get("search_profiles", [])
        lookback_days = int(user_cfg.get("lookback_days", 7))
        mindate, maxdate = _date_range(lookback_days)

        run_status = "success"
        error_message = None
        profile_details: list[dict] = []
        user_id = None

        try:
            with conn_ctx(db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    print(f"[fetch] User {username} not in DB, skipping.")
                    continue
                user_id = row["id"]

            for profile in profiles:
                profile_name = profile.get("name", "unnamed")
                if not profile.get("enabled", True):
                    print(f"[fetch] {username}/{profile_name}: skipped (disabled)")
                    continue
                query = profile.get("query", "").strip()
                if not query:
                    continue
                print(f"[fetch] {username}/{profile_name}: querying PubMed ({mindate} to {maxdate})")

                pf_found = 0
                pf_new = 0
                pf_filtered = 0
                pf_error = None

                try:
                    pmids = search_pubmed(query, mindate, maxdate, api_key)
                except Exception as e:
                    pf_error = str(e)
                    print(f"[fetch] esearch error for {profile_name}: {e}", file=sys.stderr)
                    profile_details.append({
                        "profile": profile_name,
                        "found": 0, "new": 0, "filtered": 0,
                        "error": pf_error,
                    })
                    continue

                pf_found = len(pmids)
                if not pmids:
                    print(f"[fetch] {username}/{profile_name}: 0 results")
                    profile_details.append({
                        "profile": profile_name,
                        "found": 0, "new": 0, "filtered": 0,
                    })
                    continue

                # Batch fetch in groups of 200
                for i in range(0, len(pmids), 200):
                    batch = pmids[i:i+200]
                    try:
                        xml_root = fetch_pubmed_records(batch, api_key)
                    except Exception as e:
                        print(f"[fetch] efetch error: {e}", file=sys.stderr)
                        continue
                    time.sleep(0.12)  # stay under rate limit

                    for article_el in xml_root.findall(".//PubmedArticle"):
                        record = parse_article(article_el)
                        if record is None:
                            continue

                        norm_issn = _norm_issn(record["issn"])
                        scopus_data = scopus.get(norm_issn)
                        quartile, cites_2yr, cites_3yr, sjr, publisher, h_index = scopus_data if scopus_data else (None, None, None, None, None, None)
                        record["scopus_quartile"] = quartile
                        record["scopus_cites_per_doc"] = cites_2yr
                        record["scopus_cites_3yr"] = cites_3yr
                        record["scopus_sjr"] = sjr
                        record["publisher"] = publisher
                        record["scopus_h_index"] = h_index

                        if q2_hard and quartile in ("Q3", "Q4"):
                            pf_filtered += 1
                            continue

                        with conn_ctx(db_path) as conn:
                            existing = conn.execute(
                                "SELECT pmid FROM papers WHERE pmid = ?",
                                (record["pmid"],),
                            ).fetchone()

                            if existing is None:
                                oa_url = get_oa_url(record["doi"], ncbi_email)
                                record["oa_url"] = oa_url
                                record["first_seen_at"] = now_iso
                                conn.execute(
                                    """INSERT INTO papers
                                       (pmid, title, authors, journal, issn,
                                        pub_date, epub_date, abstract, doi,
                                        oa_url, mesh_terms, scopus_quartile,
                                        scopus_cites_per_doc, scopus_cites_3yr,
                                        scopus_sjr, scopus_h_index, publisher, first_seen_at)
                                       VALUES
                                       (:pmid,:title,:authors,:journal,:issn,
                                        :pub_date,:epub_date,:abstract,:doi,
                                        :oa_url,:mesh_terms,:scopus_quartile,
                                        :scopus_cites_per_doc,:scopus_cites_3yr,
                                        :scopus_sjr,:scopus_h_index,:publisher,:first_seen_at)""",
                                    record,
                                )
                                pf_new += 1

                            conn.execute(
                                """INSERT OR IGNORE INTO user_papers
                                   (user_id, pmid, is_read, is_starred, added_at, search_profile)
                                   VALUES (?, ?, 0, 0, ?, ?)""",
                                (user_id, record["pmid"], now_iso, profile_name),
                            )

                print(
                    f"[fetch] {username}/{profile_name}: "
                    f"found={pf_found}, filtered={pf_filtered}, new={pf_new}"
                )
                profile_details.append({
                    "profile": profile_name,
                    "found": pf_found,
                    "filtered": pf_filtered,
                    "new": pf_new,
                })

        except Exception as e:
            import traceback
            run_status = "error"
            error_message = traceback.format_exc()
            print(f"[fetch] FATAL ERROR for {username}:\n{error_message}", file=sys.stderr)
            if user_email and user_id is not None:
                send_error_email(
                    config, db_path, user_id, user_email, "Fetch error",
                    f"{app_name} fetch failed for {username}:\n\n{error_message}",
                )

        total_found = sum(p.get("found", 0) for p in profile_details)
        total_new = sum(p.get("new", 0) for p in profile_details)

        if user_id is not None:
            try:
                with conn_ctx(db_path) as conn:
                    conn.execute(
                        """INSERT INTO fetch_log
                           (user_id, run_at, papers_found, papers_new,
                            status, error_message, details)
                           VALUES (?,?,?,?,?,?,?)""",
                        (user_id, now_iso, total_found, total_new,
                         run_status, error_message, json.dumps(profile_details)),
                    )
            except Exception as e:
                print(f"[fetch] Could not write fetch_log: {e}", file=sys.stderr)

        print(
            f"[fetch] {username}: found={total_found}, new={total_new}, "
            f"status={run_status}"
        )


if __name__ == "__main__":
    # Add project root to path so 'from app import db' works
    sys.path.insert(0, str(BASE_DIR))
    main()
