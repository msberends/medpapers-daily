#!/usr/bin/env python3
"""
Standalone cron script: fetch new papers from PubMed, filter by Scopus
quartile, and store them in the SQLite database.
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


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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

def _parse_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


_SCOPUS_EXTENDED = BASE_DIR / "data" / "scopus_extended.csv"
_EXTENDED_FIELDNAMES = ["issn", "eIssn", "title", "publisher", "quartile",
                        "citescore", "percentile"]


def _load_csv_into_mapping(
    path: Path,
    mapping: dict[str, tuple],
) -> None:
    """Parse one Scopus CSV file and populate mapping in-place."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
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


def load_scopus() -> dict[str, tuple[str, float | None, float | None, str | None, str | None]]:
    """Returns ISSN (hyphen-free) -> (quartile, citescore, percentile, publisher, title).

    Reads the main scopus_journals.csv (produced by fetch_scopus_journals.py) and the
    supplementary scopus_extended.csv (ISSN lookups accumulated by this script at fetch
    time for any journal not covered by the main sweep).
    """
    mapping: dict[str, tuple[str, float | None, float | None, str | None, str | None]] = {}
    p = BASE_DIR / "data" / "scopus_journals.csv"
    if not p.exists():
        print(f"[fetch] {_ts()}  WARNING: {p} not found. Quartile filtering disabled.")
        return mapping
    _load_csv_into_mapping(p, mapping)
    # Overlay any individually-resolved journals discovered at fetch time.
    _load_csv_into_mapping(_SCOPUS_EXTENDED, mapping)
    return mapping


def scopus_lookup_by_issn(
    issn: str,
    api_key: str,
    mapping: dict[str, tuple],
) -> tuple | None:
    """Query the Elsevier Serial Title API for a single ISSN and update the mapping.

    The Elsevier Serial Title API returns results for any valid ISSN regardless of
    subject-area ordering or the 10,000-result pagination cap that afflicts broad
    subject queries. This is therefore the only reliable way to resolve journals
    that fall in the invisible tail of a capped subject result set (e.g. MEDI).

    On success the result is written to scopus_extended.csv so subsequent fetch
    runs read it from disk without making another API call.
    """
    if not api_key or not issn:
        return None
    norm = _norm_issn(issn)
    if norm in mapping:
        return mapping[norm]
    url = "https://api.elsevier.com/content/serial/title"
    try:
        r = requests.get(
            url,
            params={"issn": issn, "view": "CITESCORE"},
            headers={"Accept": "application/json", "X-ELS-APIKey": api_key},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        entries = r.json().get("serial-metadata-response", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            return None
        entry = entries[0]
        csyl = entry.get("citeScoreYearInfoList", {})
        cs_year = csyl.get("citeScoreCurrentMetricYear")
        if not cs_year:
            return None

        # Derive best percentile (same logic as fetch_scopus_journals.py)
        best_pct = None
        for yi in ([csyl.get("citeScoreYearInfo")] if isinstance(csyl.get("citeScoreYearInfo"), dict)
                   else (csyl.get("citeScoreYearInfo") or [])):
            if yi.get("@year") != cs_year or yi.get("@status") != "Complete":
                continue
            for ci_block in ([csyl_ci] if isinstance(
                (csyl_ci := yi.get("citeScoreInformationList", [])), dict) else csyl_ci):
                for ci in ([ci_block.get("citeScoreInfo")] if isinstance(
                    ci_block.get("citeScoreInfo"), dict) else (ci_block.get("citeScoreInfo") or [])):
                    for sr in ([ci.get("citeScoreSubjectRank")] if isinstance(
                        ci.get("citeScoreSubjectRank"), dict)
                        else (ci.get("citeScoreSubjectRank") or [])):
                        try:
                            pct = float(sr.get("percentile", -1))
                        except (TypeError, ValueError):
                            pct = -1
                        if pct > (best_pct if best_pct is not None else -1):
                            best_pct = pct

        if best_pct is None:
            return None

        q_num = (1 if best_pct >= 75 else 2 if best_pct >= 50 else 3 if best_pct >= 25 else 4)
        quartile   = f"Q{q_num}"
        citescore  = csyl.get("citeScoreCurrentMetric")
        publisher  = (entry.get("dc:publisher") or "").strip() or None
        title      = (entry.get("dc:title")     or "").strip() or None
        print_issn = (entry.get("prism:issn")   or "").strip()
        e_issn     = (entry.get("prism:eIssn")  or "").strip()

        try:
            citescore_f: float | None = float(citescore) if citescore else None
        except (TypeError, ValueError):
            citescore_f = None

        result: tuple = (quartile, citescore_f, best_pct, publisher, title)

        # Update in-memory mapping for both ISSNs
        for raw in (print_issn, e_issn):
            n = _norm_issn(raw)
            if raw and n not in mapping:
                mapping[n] = result

        # Persist to extended CSV so future fetches don't need another API call
        write_header = not _SCOPUS_EXTENDED.exists()
        with open(_SCOPUS_EXTENDED, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_EXTENDED_FIELDNAMES, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow({
                "issn":       print_issn,
                "eIssn":      e_issn,
                "title":      title or "",
                "publisher":  publisher or "",
                "quartile":   q_num,
                "citescore":  citescore_f if citescore_f is not None else "",
                "percentile": best_pct,
            })
        print(f"[fetch] {_ts()}  Scopus extended: resolved {issn} → {title!r} {quartile}")
        return result

    except Exception as e:
        print(f"[fetch] {_ts()}  Scopus ISSN lookup failed for {issn}: {e}", file=sys.stderr)
        return None


# ── PubMed fetching ───────────────────────────────────────────────────────────

def _date_range(lookback_days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    return str(start).replace("-", "/"), str(today).replace("-", "/")


from app.pubmed import (  # noqa: E402 — imported after BASE_DIR is set
    search_pubmed,
    fetch_pubmed_records,
    parse_article,
    upsert_paper,
    _norm_issn,
)


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
    main_start = time.perf_counter()
    config = load_config()
    api_key = config.get("ncbi_api_key", "")
    ncbi_email = config.get("ncbi_email", "anonymous@example.com")
    elsevier_api_key = config.get("elsevier_api_key", "")
    db_path = str(BASE_DIR / config.get("db_path", "data/paperdigest.db"))
    scopus = load_scopus()
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"[fetch] {_ts()}  Starting fetch.")
    users = load_user_configs(config)
    if not users:
        print(f"[fetch] {_ts()}  No users with fetch_enabled found.")
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
            print(f"[fetch] {_ts()}  Backfilled Scopus fields for {updated} papers.")

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
            print(f"[fetch] {_ts()}  Updated journal names from Scopus for {updated} papers.")

    # Backfill titles that were truncated by old XML parsing (text before first <i> child only)
    with conn_ctx(db_path) as conn:
        short_pmids = [
            r["pmid"] for r in conn.execute(
                "SELECT pmid FROM papers WHERE length(trim(title)) < 30"
            ).fetchall()
        ]
    if short_pmids:
        print(f"[fetch] {_ts()}  Re-fetching titles for {len(short_pmids)} potentially truncated papers.")
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
            print(f"[fetch] {_ts()}  Title backfill failed: {e}", file=sys.stderr)

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
            print(f"[fetch] {_ts()}  User {username} not in DB, skipping.")
            continue

        _resolved_uid = _uid_row["id"]
        if not should_fetch_today(user_cfg, _resolved_uid, db_path):
            schedule = user_cfg.get("fetch_schedule", "daily")
            print(f"[fetch] {_ts()}  {username}: skipping (schedule={schedule}).")
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
        user_start = time.perf_counter()

        try:
            with conn_ctx(db_path) as conn:
                row = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    print(f"[fetch] {_ts()}  User {username} not in DB, skipping.")
                    continue
                user_id = row["id"]

            for profile in profiles:
                profile_name = profile.get("name", "unnamed")
                if not profile.get("enabled", True):
                    print(f"[fetch] {_ts()}  {username}/{profile_name}: skipped (disabled).")
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
                print(f"[fetch] {_ts()}  {username}/{profile_name}: querying PubMed ({mindate} to {maxdate}).")

                pf_found = 0
                pf_new = 0
                pf_filtered = 0
                pf_error = None
                profile_start = time.perf_counter()

                try:
                    pmids = search_pubmed(query, mindate, maxdate, api_key)
                except Exception as e:
                    pf_error = str(e)
                    print(f"[fetch] {_ts()}  esearch error for {profile_name}: {e}", file=sys.stderr)
                    profile_details.append({
                        "profile": profile_name,
                        "found": 0, "new": 0, "filtered": 0,
                        "error": pf_error,
                    })
                    continue

                pf_found = len(pmids)
                if not pmids:
                    pf_elapsed = time.perf_counter() - profile_start
                    print(f"[fetch] {_ts()}  {username}/{profile_name}: 0 results ({pf_elapsed:.1f} sec).")
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
                        print(f"[fetch] {_ts()}  efetch error: {e}", file=sys.stderr)
                        continue
                    time.sleep(0.12)  # stay under rate limit

                    for article_el in xml_root.findall(".//PubmedArticle"):
                        record = parse_article(article_el)
                        if record is None:
                            continue

                        norm_issn = _norm_issn(record["issn"])
                        scopus_data = scopus.get(norm_issn)
                        if scopus_data is None and record["issn"] and elsevier_api_key:
                            scopus_data = scopus_lookup_by_issn(record["issn"], elsevier_api_key, scopus)
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

                pf_elapsed = time.perf_counter() - profile_start
                print(
                    f"[fetch] {_ts()}  {username}/{profile_name}: "
                    f"found={pf_found}, filtered={pf_filtered}, new={pf_new} "
                    f"({pf_elapsed:.1f} sec)."
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
            print(f"[fetch] {_ts()}  FATAL ERROR for {username}:\n{error_message}", file=sys.stderr)
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
                print(f"[fetch] {_ts()}  Could not write fetch_log: {e}", file=sys.stderr)

        user_elapsed = time.perf_counter() - user_start
        print(
            f"[fetch] {_ts()}  {username}: found={total_found}, new={total_new}, "
            f"status={run_status}, time: {user_elapsed:.1f} sec."
        )

    _auto_fetch_staff_papers(config, db_path, scopus)
    _launch_llm_highlights(config)
    _clean_expired_sessions(db_path)
    total_elapsed = time.perf_counter() - main_start
    print(f"[fetch] {_ts()}  Done. Total time: {total_elapsed:.1f} sec.")


def _auto_fetch_staff_papers(config: dict, db_path: str, scopus: dict) -> None:
    """Fetch and auto-confirm new papers for active staff who already have confirmed publications.

    Uses a date-limited PubMed name/ORCID search (not the full Computed Authors API) so that
    only recently published papers are retrieved on each cron run. New PMIDs not already in
    staff_papers are inserted as 'confirmed' — no manual review required for established staff.
    """
    api_key = config.get("ncbi_api_key", "")
    lookback = int(config.get("staff_fetch_lookback_days", 30))
    mindate, maxdate = _date_range(lookback)

    with conn_ctx(db_path) as conn:
        members = conn.execute(
            """SELECT s.* FROM staff s
               WHERE s.active = 1
               AND (SELECT COUNT(*) FROM staff_papers sp
                    WHERE sp.staff_id = s.id AND sp.status = 'confirmed') > 0
               ORDER BY s.name"""
        ).fetchall()

    if not members:
        return

    print(f"[fetch] {_ts()}  Staff auto-fetch: {len(members)} member(s), {lookback}-day window.")
    total_new = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for member in members:
        m = dict(member)
        with conn_ctx(db_path) as conn:
            already = {
                r["pmid"] for r in conn.execute(
                    "SELECT pmid FROM staff_papers WHERE staff_id = ?", (m["id"],)
                ).fetchall()
            }

        if m.get("orcid"):
            query = f"{m['orcid']}[auid]"
        else:
            query = f"{m['author_last']} {m['author_initials']}[au]"

        try:
            candidate_pmids = search_pubmed(query, mindate, maxdate, api_key, retmax=200)
        except Exception as e:
            print(f"[fetch] {_ts()}  Staff auto-fetch: search failed for {m['name']}: {e}", file=sys.stderr)
            continue

        new_pmids = [p for p in candidate_pmids if p not in already]
        if not new_pmids:
            continue

        member_new = 0
        for i in range(0, len(new_pmids), 200):
            batch = new_pmids[i : i + 200]
            try:
                xml_root = fetch_pubmed_records(batch, api_key)
            except Exception as e:
                print(f"[fetch] {_ts()}  Staff auto-fetch: efetch failed for {m['name']}: {e}", file=sys.stderr)
                continue
            time.sleep(0.12)

            for article_el in xml_root.findall(".//PubmedArticle"):
                record = parse_article(article_el)
                if record is None:
                    continue
                sd = scopus.get(_norm_issn(record["issn"]))
                record["scopus_quartile"]   = sd[0] if sd else None
                record["scopus_citescore"]  = sd[1] if sd else None
                record["scopus_percentile"] = sd[2] if sd else None
                record["publisher"]         = sd[3] if sd else None
                if sd and sd[4]:
                    record["journal"] = sd[4]

                with conn_ctx(db_path) as conn:
                    upsert_paper(conn, record, scopus)
                    result = conn.execute(
                        """INSERT OR IGNORE INTO staff_papers (staff_id, pmid, status, reviewed_at)
                           VALUES (?, ?, 'confirmed', ?)""",
                        (m["id"], record["pmid"], now_iso),
                    )
                    if result.rowcount:
                        member_new += 1

        if member_new:
            print(f"[fetch] {_ts()}  Staff auto-fetch: {m['name']}: {member_new} new paper(s) confirmed.")
        total_new += member_new

    msg = f"{total_new} total new paper(s) confirmed." if total_new else "no new papers found."
    print(f"[fetch] {_ts()}  Staff auto-fetch: {msg}")


def _clean_expired_sessions(db_path: str):
    """Remove expired session rows — kept small so a restarting server never accumulates them."""
    try:
        with conn_ctx(db_path) as conn:
            deleted = conn.execute(
                "DELETE FROM sessions WHERE expires_at < datetime('now')"
            ).rowcount
        if deleted:
            print(f"[fetch] {_ts()}  Cleaned {deleted} expired session(s).")
    except Exception as e:
        print(f"[fetch] {_ts()}  Could not clean sessions: {e}", file=sys.stderr)


def _launch_llm_highlights(config: dict):
    """Fire-and-forget: launch llm_highlights.py in the background if an LLM is configured for highlights."""
    from app.llm import get_provider_config
    if get_provider_config(config, "highlights") is None:
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
        print(f"[fetch] {_ts()}  LLM highlights generation queued in background.")
    except Exception as e:
        print(f"[fetch] {_ts()}  Could not start LLM highlights: {e}", file=sys.stderr)


if __name__ == "__main__":
    # Add project root to path so 'from app import db' works
    sys.path.insert(0, str(BASE_DIR))
    main()
