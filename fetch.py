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


def should_fetch_today(user_cfg: dict, user_id: int | None = None, db_path: str = "") -> bool:
    schedule = user_cfg.get("fetch_schedule", "daily")
    if schedule == "daily":
        return True
    today = datetime.now(timezone.utc).date()

    # Determine the required interval in days
    if schedule == "weekly":
        required_days = 7
    elif schedule == "monthly":
        required_days = 28
    else:
        return True

    # If we know the user and have a DB, check when the last successful fetch ran.
    # Fetch if elapsed time exceeds the required interval, so a missed day is recovered.
    if user_id and db_path:
        try:
            with conn_ctx(db_path) as conn:
                row = conn.execute(
                    """SELECT run_at FROM fetch_log
                       WHERE user_id = ? AND status = 'success'
                       ORDER BY run_at DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
            if row:
                last_run = datetime.fromisoformat(row["run_at"].replace("Z", "+00:00")).date()
                elapsed = (today - last_run).days
                return elapsed >= required_days
        except Exception:
            pass  # Fall through to day-of-week/month check

    # Fallback: check the scheduled day (original behaviour)
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


def load_scopus() -> dict[str, tuple[str, float | None, float | None, str | None, str | None]]:
    """Returns ISSN (hyphen-free) -> (quartile, citescore, percentile, publisher, title).

    Reads data/scopus_journals.csv produced by fetch_scopus_journals.py.
    Columns used: issn, eIssn, quartile (numeric 1-4), citescore, percentile, publisher, title.
    """
    mapping: dict[str, tuple[str, float | None, float | None, str | None, str | None]] = {}
    p = BASE_DIR / "data" / "scopus_journals.csv"
    if not p.exists():
        print(f"[fetch] WARNING: {p} not found. Quartile filtering disabled.")
        return mapping
    with open(p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                q_num = int(float(row.get("quartile") or ""))
                quartile = f"Q{q_num}" if 1 <= q_num <= 4 else None
            except (ValueError, TypeError):
                quartile = None
            if quartile is None:
                continue
            citescore  = _parse_float(row.get("citescore")  or "")
            percentile = _parse_float(row.get("percentile") or "")
            publisher  = (row.get("publisher") or "").strip() or None
            title      = (row.get("title")     or "").strip() or None
            for issn_field in (row.get("issn") or "", row.get("eIssn") or ""):
                for raw_issn in issn_field.split(","):
                    raw_issn = raw_issn.strip()
                    if raw_issn:
                        norm = _norm_issn(raw_issn)
                        if norm not in mapping:
                            mapping[norm] = (quartile, citescore, percentile, publisher, title)
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
    abstract_sections = []
    for node in art.findall(".//AbstractText"):
        text = "".join(node.itertext()).strip()
        if not text:
            continue
        raw_label = (node.get("Label") or "").strip()
        label = raw_label.title() if raw_label else ""
        abstract_sections.append({"label": label, "text": text})
    abstract = " ".join(s["text"] for s in abstract_sections)
    has_labels = any(s["label"] for s in abstract_sections)
    abstract_structured = json.dumps(abstract_sections) if has_labels else None

    # Authors + affiliations
    authors = []
    author_affil_raw = []  # per-author list of affiliation strings
    for auth in art.findall(".//Author"):
        ln = _text(auth, "LastName")
        fn = _text(auth, "ForeName") or _text(auth, "Initials")
        affils = [
            el.text.strip()
            for el in auth.findall("AffiliationInfo/Affiliation")
            if el.text
        ]
        if ln:
            authors.append(f"{ln}, {fn}".strip(", "))
            author_affil_raw.append(affils)
        else:
            cn = _text(auth, "CollectiveName")
            if cn:
                authors.append(cn)
                author_affil_raw.append(affils)

    # Deduplicate affiliations, preserving order
    aff_list: list[str] = []
    aff_index: dict[str, int] = {}
    for affils in author_affil_raw:
        for a in affils:
            if a not in aff_index:
                aff_index[a] = len(aff_list)
                aff_list.append(a)
    author_aff = [[aff_index[a] for a in affils] for affils in author_affil_raw]
    affiliations = json.dumps({"aff_list": aff_list, "author_aff": author_aff}) if aff_list else None

    # Journal
    journal_el = art.find("Journal")
    journal_name = ""
    iso_abbreviation = ""
    issn = ""
    if journal_el is not None:
        iso_abbreviation = _text(journal_el, "ISOAbbreviation") or ""
        journal_name = _text(journal_el, "Title") or iso_abbreviation
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

    # Author-provided keywords (often present before MeSH indexing is complete).
    # KeywordList is a child of MedlineCitation, not of Article.
    keywords = [
        node.text.strip()
        for node in medline.findall(".//KeywordList/Keyword")
        if node.text
    ]

    return {
        "pmid": pmid,
        "title": title,
        "authors": json.dumps(authors),
        "affiliations": affiliations,
        "journal": journal_name,
        "iso_abbreviation": iso_abbreviation or None,
        "issn": issn,
        "pub_date": pub_date,
        "epub_date": epub_date,
        "abstract": abstract,
        "abstract_structured": abstract_structured,
        "doi": doi,
        "mesh_terms": json.dumps(mesh_terms),
        "keywords": json.dumps(keywords),
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
    app_name = config.get("app_name", "MedPapers Daily")
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
    scopus = load_scopus()
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
                   WHERE (scopus_citescore IS NULL OR scopus_percentile IS NULL OR publisher IS NULL)
                     AND issn IS NOT NULL"""
            ).fetchall()
            updated = 0
            for row in to_fix:
                data = scopus.get(_norm_issn(row["issn"]))
                if data:
                    _, citescore, percentile, pub, _ = data
                    conn.execute(
                        """UPDATE papers
                           SET scopus_citescore  = COALESCE(scopus_citescore,  ?),
                               scopus_percentile = COALESCE(scopus_percentile, ?),
                               publisher         = COALESCE(publisher,         ?)
                           WHERE pmid = ?""",
                        (citescore, percentile, pub, row["pmid"]),
                    )
                    updated += 1
        if updated:
            print(f"[fetch] Backfilled Scopus fields for {updated} papers")

    # Sync journal names from Scopus — runs on every fetch so a CSV refresh propagates automatically
    if scopus:
        with conn_ctx(db_path) as conn:
            rows = conn.execute(
                "SELECT pmid, issn, journal FROM papers WHERE issn IS NOT NULL"
            ).fetchall()
            updated = 0
            for row in rows:
                data = scopus.get(_norm_issn(row["issn"]))
                if data:
                    _, _, _, _, scopus_title = data
                    if scopus_title and scopus_title != row["journal"]:
                        conn.execute("UPDATE papers SET journal = ? WHERE pmid = ?",
                                     (scopus_title, row["pmid"]))
                        updated += 1
        if updated:
            print(f"[fetch] Updated journal names from Scopus for {updated} papers")

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
        # Resolve user_id first so should_fetch_today can check fetch_log
        _uid_row = None
        try:
            with conn_ctx(db_path) as conn:
                _uid_row = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
        except Exception:
            pass
        if _uid_row is None:
            print(f"[fetch] User {username} not in DB, skipping.")
            continue

        _resolved_uid = _uid_row["id"]
        if not should_fetch_today(user_cfg, _resolved_uid, db_path):
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
        user_id = _resolved_uid

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

                # Upsert this profile into search_profiles and get its stable integer ID
                enabled_int = 1 if profile.get("enabled", True) else 0
                with conn_ctx(db_path) as conn:
                    conn.execute(
                        """INSERT INTO search_profiles (user_id, name, query, enabled, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(user_id, name) DO UPDATE
                           SET query=excluded.query, enabled=excluded.enabled""",
                        (user_id, profile_name, query, enabled_int, now_iso),
                    )
                    sp_row = conn.execute(
                        "SELECT id FROM search_profiles WHERE user_id=? AND name=?",
                        (user_id, profile_name),
                    ).fetchone()
                    sp_id = sp_row["id"] if sp_row else None
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
                        quartile, citescore, percentile, publisher, scopus_title = scopus_data if scopus_data else (None, None, None, None, None)
                        record["scopus_quartile"]   = quartile
                        record["scopus_citescore"]  = citescore
                        record["scopus_percentile"] = percentile
                        record["publisher"]         = publisher
                        if scopus_title:
                            record["journal"] = scopus_title

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
                                       (pmid, title, authors, affiliations, journal, iso_abbreviation, issn,
                                        pub_date, epub_date, abstract, abstract_structured, doi,
                                        oa_url, mesh_terms, keywords,
                                        scopus_quartile, scopus_citescore,
                                        scopus_percentile, publisher, first_seen_at)
                                       VALUES
                                       (:pmid,:title,:authors,:affiliations,:journal,:iso_abbreviation,:issn,
                                        :pub_date,:epub_date,:abstract,:abstract_structured,:doi,
                                        :oa_url,:mesh_terms,:keywords,
                                        :scopus_quartile,:scopus_citescore,
                                        :scopus_percentile,:publisher,:first_seen_at)""",
                                    record,
                                )
                                pf_new += 1
                            else:
                                # Refresh metadata on existing papers: MeSH and keywords
                                # are added by NLM weeks after first fetch; abstract_structured
                                # is populated here for papers fetched before this feature existed.
                                conn.execute(
                                    """UPDATE papers
                                       SET mesh_terms=?, keywords=?, affiliations=?,
                                           abstract=?, abstract_structured=?
                                       WHERE pmid=?""",
                                    (record["mesh_terms"], record["keywords"], record["affiliations"],
                                     record["abstract"], record["abstract_structured"], record["pmid"]),
                                )

                            conn.execute(
                                """INSERT OR IGNORE INTO user_papers
                                   (user_id, pmid, is_read, is_starred, added_at,
                                    search_profile, search_profile_id)
                                   VALUES (?, ?, 0, 0, ?, ?, ?)""",
                                (user_id, record["pmid"], now_iso, profile_name, sp_id),
                            )
                            # Record this profile match regardless of which profile
                            # "won" the user_papers primary attribution above.
                            conn.execute(
                                """INSERT OR IGNORE INTO user_paper_profiles
                                   (user_id, pmid, profile_id, added_at)
                                   VALUES (?, ?, ?, ?)""",
                                (user_id, record["pmid"], sp_id, now_iso),
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

    _launch_llm_highlights(config)
    _clean_expired_sessions(db_path)


def _clean_expired_sessions(db_path: str):
    """Remove expired session rows — kept small so a restarting server never accumulates them."""
    try:
        with conn_ctx(db_path) as conn:
            deleted = conn.execute(
                "DELETE FROM sessions WHERE expires_at < datetime('now')"
            ).rowcount
        if deleted:
            print(f"[fetch] Cleaned {deleted} expired session(s).")
    except Exception as e:
        print(f"[fetch] Could not clean sessions: {e}", file=sys.stderr)


def _launch_llm_highlights(config: dict):
    """Fire-and-forget: launch llm_highlights.py in the background if an LLM is configured."""
    if not config.get("llm_provider"):
        return
    try:
        import subprocess
        venv_py = BASE_DIR / "venv" / "bin" / "python"
        llm_script = BASE_DIR / "llm_highlights.py"
        log_path = BASE_DIR / "logs" / "llm.log"
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [str(venv_py), "-u", str(llm_script)],
                stdout=log_f,
                stderr=log_f,
                cwd=str(BASE_DIR),
            )
        print("[fetch] LLM highlights generation queued in background.")
    except Exception as e:
        print(f"[fetch] Could not start LLM highlights: {e}", file=sys.stderr)


if __name__ == "__main__":
    # Add project root to path so 'from app import db' works
    sys.path.insert(0, str(BASE_DIR))
    main()
