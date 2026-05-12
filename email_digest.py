#!/usr/bin/env python3
"""
Standalone cron script: compose and send the daily HTML email digest
to each user who has new, unactioned papers.

Invoked 15 minutes after fetch.py; does NOT import from app/.
"""
import json
import os
import re
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import yaml

BASE_DIR = Path(__file__).parent


# ── Config ────────────────────────────────────────────────────────────────────

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


# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


# ── Topic classification ──────────────────────────────────────────────────────

def classify(mesh_terms_json: str, mesh_topic_map: dict) -> list[str]:
    if not mesh_terms_json:
        return []
    terms = json.loads(mesh_terms_json)
    topics = set()
    for t in terms:
        if t in mesh_topic_map:
            topics.add(mesh_topic_map[t])
    return sorted(topics)


# ── Email composition ──────────────────────────────────────────────────────────

# bg, border — using Bootstrap subtle colours (hardcoded for email-client compatibility)
QUARTILE_COLORS = {
    "Q1": ("#d9eee1", "#b4dec3"),
    "Q2": ("#cce8f1", "#99d1e3"),
    "Q3": ("#fbe9cc", "#f6d39a"),
    "Q4": ("#fcd9d3", "#f9b3a7"),
}

TOPIC_COLORS = [
    ("#ede0ff", "#c4a8f8"),  # violet
    ("#ffe0ef", "#f4a8cc"),  # rose
    ("#ccf4ea", "#80d8b8"),  # teal
    ("#fff3cc", "#f5d87a"),  # amber
    ("#dce8ff", "#a8c0f5"),  # sky
    ("#e2f0d8", "#a8d490"),  # sage
    ("#ffe8dc", "#f4b89a"),  # coral
    ("#f0dcff", "#d498f8"),  # plum
]


def _topic_color(topic: str) -> tuple[str, str]:
    idx = sum(ord(c) for c in topic) % len(TOPIC_COLORS)
    return TOPIC_COLORS[idx]


def _badge(text: str, bg: str, border: str = "") -> str:
    border_css = f"border:1px solid {border};" if border else ""
    return (f'<span style="display:inline-block;padding:2px 7px;border-radius:4px;'
            f'background:{bg};{border_css}color:#333;font-size:.8em;font-weight:600">{text}</span>')


def _author_summary(authors_json: str) -> str:
    authors = json.loads(authors_json or "[]")
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]}; {authors[1]}"
    return f"{authors[0]} et al. (last: {authors[-1]})"


def _proxy_url(doi: str, config: dict) -> str | None:
    if not config.get("proxy_enabled") or not doi:
        return None
    proxy_domain = (config.get("proxy_domain") or "").strip()
    if not proxy_domain:
        return None
    url = f"https://doi.org/{doi}"
    url_regex = (config.get("proxy_url_regex") or "").strip()
    if url_regex:
        try:
            if not re.search(url_regex, url):
                return None
        except re.error:
            return None
    parsed = urlparse(url)
    dashed_host = parsed.netloc.replace(".", "-")
    return urlunparse(("https", f"{dashed_host}.{proxy_domain}", parsed.path, "", "", ""))


def build_html_digest(papers: list, mesh_topic_map: dict, base_url: str,
                      username: str, today_str: str, config: dict | None = None,
                      journal_metric: str = "if") -> str:
    rows_html = ""
    for p in papers:
        pmid = p["pmid"]
        title = p["title"]
        authors_str = _author_summary(p["authors"])
        journal = p["journal"]
        pub_date = p["pub_date"] or ""
        quartile = p["scopus_quartile"] or ""
        abstract = p["abstract"] or ""
        doi = p["doi"] or ""
        oa_url = p["oa_url"] or ""
        topics = classify(p["mesh_terms"], mesh_topic_map)
        if not topics:
            topics = ["Unclassified"]

        paper_url = f"{base_url}/paper/{pmid}"
        pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"

        if journal_metric == "citescore":
            metric_val = p.get("scopus_cites_3yr")
            metric_lbl = "CiteScore"
        elif journal_metric == "sjr":
            metric_val = p.get("scopus_sjr")
            metric_lbl = "SJR"
        else:
            metric_val = p.get("scopus_cites_per_doc")
            metric_lbl = "IF"
        metric_str = f" ({metric_lbl} {metric_val:.2f})" if metric_val else ""
        q_label = f"{quartile}{metric_str}" if quartile else ""

        q_colors = QUARTILE_COLORS.get(quartile)
        q_badge = _badge(q_label, *q_colors) if q_colors else (
            _badge(q_label, "#fcfcfc", "#f8f8f8") if q_label else "")
        if oa_url:
            access_badge = _badge("Open Access", "#d9eee1", "#b4dec3")
        elif doi:
            access_badge = _badge("&#128274; PW", "#fcd9d3", "#f9b3a7")
        else:
            access_badge = ""
        topic_badges = " ".join(_badge(t, *_topic_color(t)) for t in topics)
        proxy_link = _proxy_url(doi, config or {}) if not oa_url else None

        doi_link = (f'<a href="https://doi.org/{doi}" style="color:#555;font-size:.85em">'
                    f'DOI: {doi}</a>' if doi else "")

        rows_html += f"""
<div style="border:1px solid #dee2e6;border-radius:6px;padding:16px;margin-bottom:16px;background:#fff">
  <div style="margin-bottom:6px">
    {q_badge} {access_badge} {topic_badges}
  </div>
  <h3 style="margin:0 0 6px;font-size:1em;font-weight:700">
    <a href="{paper_url}" style="color:#0d6efd;text-decoration:none">{title}</a>
  </h3>
  <p style="margin:0 0 4px;color:#555;font-size:.9em">{authors_str}</p>
  <p style="margin:0 0 8px;color:#555;font-size:.9em">
    <em>{journal}</em>
    {f'&bull; {pub_date}' if pub_date else ''}
    {f'&bull; {doi_link}' if doi_link else ''}
  </p>
  {'<details><summary style="cursor:pointer;color:#6c757d;font-size:.85em">Show abstract</summary>'
   f'<p style="margin:8px 0 0;font-size:.9em;color:#333">{abstract}</p></details>' if abstract else ''}
  <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
    <a href="{pubmed_url}" style="padding:4px 10px;background:#6c757d;color:#fff;border-radius:4px;text-decoration:none;font-size:.85em">View on PubMed</a>
    <a href="{paper_url}" style="padding:4px 10px;background:#0d6efd;color:#fff;border-radius:4px;text-decoration:none;font-size:.85em">View on Papers Daily</a>
    {f'<a href="{oa_url}" style="padding:4px 10px;background:#198754;color:#fff;border-radius:4px;text-decoration:none;font-size:.85em">Full Text</a>' if oa_url else ''}
    {f'<a href="{proxy_link}" style="padding:4px 10px;background:#dc3545;color:#fff;border-radius:4px;text-decoration:none;font-size:.85em">&#128274; Full Text via Proxy</a>' if proxy_link else ''}
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#212529;background:#f8f9fa">
  <div style="background:#0d6efd;color:#fff;padding:20px;border-radius:8px;margin-bottom:24px">
    <h1 style="margin:0;font-size:1.4em">Papers Daily Digest</h1>
    <p style="margin:6px 0 0;opacity:.85">{today_str} &bull; {len(papers)} new paper(s) for {username}</p>
  </div>
  {rows_html}
  <hr style="margin:24px 0;border-color:#dee2e6">
  <p style="color:#adb5bd;font-size:.8em;text-align:center">
    Sent by <a href="{base_url}" style="color:#adb5bd">Papers Daily</a> &bull;
    <a href="{base_url}/settings" style="color:#adb5bd">Manage preferences</a>
  </p>
</body>
</html>"""


def build_plain_digest(papers: list, base_url: str) -> str:
    lines = [f"Papers Daily Digest — {len(papers)} new paper(s)\n", "=" * 60]
    for p in papers:
        lines.append(f"\n{p['title']}")
        authors = json.loads(p["authors"] or "[]")
        if authors:
            lines.append(f"  {authors[0]}" + (f" et al." if len(authors) > 1 else ""))
        lines.append(f"  {p['journal']} | {p['pub_date'] or ''}")
        lines.append(f"  PubMed: https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}")
        lines.append(f"  Papers Daily: {base_url}/paper/{p['pmid']}")
        lines.append("-" * 40)
    return "\n".join(lines)


# ── Send ──────────────────────────────────────────────────────────────────────

def _log_mail(db_path: str, user_id: int, to: str, subject: str,
              status: str, error: str = None):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with conn_ctx(db_path) as conn:
            conn.execute(
                """INSERT INTO mail_log (user_id, sent_at, to_addr, subject, status, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, now, to, subject, status, error),
            )
    except Exception as e:
        print(f"[email] Failed to write mail_log: {e}", file=sys.stderr)


def send_email(config: dict, to: str, subject: str, html: str, plain: str):
    from mail_helper import send_email as _send
    _send(config, to, subject, html, plain)


def send_error_email(config: dict, to: str, subject: str, body: str):
    from mail_helper import send_plain_email
    app_name = config.get("app_name", "Papers Daily")
    try:
        send_plain_email(config, to, f"[{app_name}] {subject}", body)
    except Exception as e:
        print(f"[email] Failed to send error email: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    db_path = str(BASE_DIR / config.get("db_path", "data/paperdigest.db"))
    base_url = config.get("base_url", "https://papers.uscloud.nl").rstrip("/")
    subject_template = config.get(
        "email_subject_template",
        "Your daily digest, {new_papers} new paper(s), {date}"
    )
    app_name = config.get("app_name", "Papers Daily")

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%d %B %Y")
    cutoff = (today - timedelta(hours=24)).isoformat()

    users = load_user_configs(config)

    for username, user_cfg in users:
        if not should_fetch_today(user_cfg):
            schedule = user_cfg.get("fetch_schedule", "daily")
            print(f"[email] {username}: skipping (schedule={schedule})")
            continue

        user_email = user_cfg.get("email", "")
        suppress_empty = user_cfg.get("email_suppress_empty", True)
        mesh_topic_map = user_cfg.get("mesh_topic_map", {})
        journal_metric = user_cfg.get("journal_metric", "if")
        user_id = None

        try:
            with conn_ctx(db_path) as conn:
                row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
                if row is None:
                    print(f"[email] User {username} not in DB, skipping.")
                    continue
                user_id = row["id"]

                # Papers that are new (added in last 24h) and unactioned
                papers_rows = conn.execute(
                    """SELECT p.* FROM papers p
                       JOIN user_papers up ON up.pmid = p.pmid
                       WHERE up.user_id = ?
                         AND up.is_read = 0
                         AND up.is_starred = 0
                         AND up.folder_id IS NULL
                         AND up.added_at >= ?
                       ORDER BY up.added_at DESC""",
                    (user_id, cutoff),
                ).fetchall()

            papers = [dict(r) for r in papers_rows]

            if not papers:
                if suppress_empty:
                    print(f"[email] {username}: no new papers, suppressing email.")
                    continue
                # Send nothing-new confirmation
                nothing_subject = f"[{app_name}] No new papers today, {today_str}"
                nothing_body = (
                    f"Papers Daily ran successfully on {today_str} "
                    f"but found no new unactioned papers for {username}.\n\n"
                    f"Visit {base_url}/feed to review your paper feed.\n"
                )
                send_email(config, user_email, nothing_subject,
                           f"<p>{nothing_body.replace(chr(10), '<br>')}</p>",
                           nothing_body)
                _log_mail(db_path, user_id, user_email, nothing_subject, "sent")
                print(f"[email] {username}: sent nothing-new notification.")
                continue

            html = build_html_digest(papers, mesh_topic_map, base_url, username, today_str, config, journal_metric)
            plain = build_plain_digest(papers, base_url)
            try:
                subject = subject_template.format(
                    new_papers=len(papers),
                    date=today_str,
                    username=username,
                    app_name=app_name,
                )
            except KeyError:
                subject = f"Your daily digest, {len(papers)} new paper(s), {today_str}"
            send_email(config, user_email, subject, html, plain)
            _log_mail(db_path, user_id, user_email, subject, "sent")
            print(f"[email] {username}: sent digest with {len(papers)} paper(s).")

        except Exception:
            err = traceback.format_exc()
            print(f"[email] ERROR for {username}:\n{err}", file=sys.stderr)
            _log_mail(db_path, user_id, user_email, "digest", "error", err[:2000])
            if user_email:
                send_error_email(
                    config, user_email, "Email digest error",
                    f"Papers Daily email digest failed for {username}:\n\n{err}",
                )


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()
