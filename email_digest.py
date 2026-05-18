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

_JOURNAL_MINOR_WORDS = frozenset({
    "a", "an", "the", "and", "but", "or", "nor", "for", "yet", "so",
    "at", "by", "in", "of", "on", "to", "up",
})


def _clean_journal(journal: str) -> str:
    """Strip PubMed subtitle and parentheticals, then apply title case."""
    s = journal or ""
    s = s.split(" : ")[0].strip()
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    words = s.split()
    return " ".join(
        w if w.isupper() else
        ((w[0].upper() + w[1:]) if i == 0 or w.lower() not in _JOURNAL_MINOR_WORDS else w.lower())
        for i, w in enumerate(words)
    )


_EMAIL_DANGER = "rgba(220,53,69,0.75)"   # BS --bs-danger at 75% opacity; hex required for email clients


def _publisher_display(publisher: str, config: dict) -> str:
    if not publisher:
        return ""
    short = (config.get("publisher_map") or {}).get(publisher, publisher)
    if publisher in (config.get("predatory_publishers") or []):
        return f'<span style="color:{_EMAIL_DANGER}">{short}</span>'
    return short


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


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


# ── Email composition ──────────────────────────────────────────────────────────

# bg, border — using Bootstrap subtle colours (hardcoded for email-client compatibility)
QUARTILE_COLORS = {
    "Q1": ("#d9eee1", "#b4dec3"),
    "Q2": ("#cce8f1", "#99d1e3"),
    "Q3": ("#fbe9cc", "#f6d39a"),
    "Q4": ("#fcd9d3", "#f9b3a7"),
}

# Inline hex required — email clients strip stylesheets and don't support CSS variables.
# Values kept close to the Bootstrap subtle-bg/border tokens they correspond to.
TOPIC_COLOUR_EMAIL: dict[str, tuple[str, str]] = {
    "red":       ("#ffe0e0", "#f4a8a8"),
    "orange":    ("#ffe8dc", "#f4b89a"),
    "yellow":    ("#fff8dc", "#f5e87a"),
    "green":     ("#ccf4ea", "#80d8b8"),
    "teal":      ("#ccf4f0", "#80d8d0"),
    "cyan":      ("#ccf0f8", "#80d0e8"),
    "blue":      ("#dce8ff", "#a8c0f5"),
    "indigo":    ("#e4d8ff", "#b89cf5"),
    "purple":    ("#ede0ff", "#c4a8f8"),
    "pink":      ("#ffe0ef", "#f4a8cc"),
    # Semantic — mapped to Bootstrap 5 default hex; won't change with Bootswatch themes
    # in email clients since CSS variables are not supported there.
    "primary":   ("#dce8ff", "#a8c0f5"),   # default primary ≈ blue
    "secondary": ("#e9ecef", "#ced4da"),
    "success":   ("#d1e7dd", "#a3cfbb"),
    "danger":    ("#f8d7da", "#f1aeb5"),
    "warning":   ("#fff3cd", "#ffecb5"),
    "info":      ("#cff4fc", "#9eeaf9"),
}

_TOPIC_COLOUR_FALLBACK = list(TOPIC_COLOUR_EMAIL.values())


def _topic_colour(topic: str, mesh_topic_colours: dict) -> tuple[str, str]:
    colour_name = mesh_topic_colours.get(topic)
    if colour_name and colour_name in TOPIC_COLOUR_EMAIL:
        return TOPIC_COLOUR_EMAIL[colour_name]
    # Fallback: cycle by hash of topic name for stable colour assignment
    idx = sum(ord(c) for c in topic) % len(_TOPIC_COLOUR_FALLBACK)
    return _TOPIC_COLOUR_FALLBACK[idx]


def _badge(text: str, bg: str, border: str = "") -> str:
    border_css = f"border:1px solid {border};" if border else ""
    return (f'<span style="display:inline-block;padding:2px 7px;border-radius:4px;'
            f'background:{bg};{border_css}color:#333;font-size:.8em;font-weight:600">{text}</span>')


_EMAIL_TERTIARY = "#adb5bd"   # BS --bs-tertiary-color; hex required for email clients


def _dim_initials(author: str) -> str:
    author = (author or "").strip()
    if "," in author:
        last, _, first = author.partition(",")
        first = first.strip()
        if first:
            return f'{last.strip()}<span style="color:{_EMAIL_TERTIARY}">, {first}</span>'
        return last.strip()
    m = re.match(r'^(.*?)(\s+[A-Z]+)$', author)
    if m:
        return f'{m.group(1)}<span style="color:{_EMAIL_TERTIARY}"> {m.group(2).strip()}</span>'
    return author


def _author_summary(authors_json: str) -> str:
    authors = json.loads(authors_json or "[]")
    if not authors:
        return ""
    if len(authors) == 1:
        return _dim_initials(authors[0])
    if len(authors) == 2:
        return f"{_dim_initials(authors[0])}; {_dim_initials(authors[1])}"
    return f"{_dim_initials(authors[0])} et al. (last: {_dim_initials(authors[-1])})"


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


_BTN_STYLE = ("display:inline-block;padding:5px 13px;border-radius:6px;"
              "text-decoration:none;font-size:.8em;font-weight:500;"
              "margin-right:8px;margin-bottom:4px")


def _render_paper_card(p: dict, mesh_topic_map: dict, mesh_topic_colours: dict,
                       base_url: str, config: dict, app_name: str) -> str:
    pmid = p["pmid"]
    title = p["title"]
    authors_str = _author_summary(p["authors"])
    journal = _clean_journal(p["journal"])
    publisher = _publisher_display(p.get("publisher") or "", config)
    pub_date = p["pub_date"] or ""
    quartile = p["scopus_quartile"] or ""
    doi = p["doi"] or ""
    oa_url = p["oa_url"] or ""
    topics = classify(p["mesh_terms"], mesh_topic_map)
    if not topics:
        topics = ["Unclassified"]

    paper_url = f"{base_url}/paper/{pmid}"
    pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"

    percentile = p.get("scopus_percentile")
    metric_str = f" ({_ordinal(int(percentile))})" if percentile else ""
    q_label = f"{quartile}{metric_str}" if quartile else ""

    q_colors = QUARTILE_COLORS.get(quartile)
    q_badge = _badge(q_label, *q_colors) if q_colors else (
        _badge(q_label, "#f8f9fa", "#dee2e6") if q_label else "")

    if oa_url:
        access_badge = _badge("Open Access", "#d9eee1", "#b4dec3")
    elif doi:
        access_badge = _badge("Paywalled", "#fcd9d3", "#f9b3a7")
    else:
        access_badge = ""

    topic_badges = " ".join(_badge(t, *_topic_colour(t, mesh_topic_colours)) for t in topics)
    proxy_link = _proxy_url(doi, config) if not oa_url else None

    meta_parts = [f"<em>{journal}</em>"]
    if publisher:
        meta_parts.append(publisher)
    if pub_date:
        meta_parts.append(pub_date)
    if doi:
        meta_parts.append(
            f'<a href="https://doi.org/{doi}" style="color:#6c757d;text-decoration:none">{doi}</a>'
        )
    meta_html = "&ensp;&middot;&ensp;".join(meta_parts)
    badges_html = "&nbsp; ".join(filter(None, [q_badge, access_badge, topic_badges]))

    btns = [
        f'<a href="{pubmed_url}" style="{_BTN_STYLE};background:#f1f3f5;color:#495057;border:1px solid #dee2e6">PubMed</a>',
        f'<a href="{paper_url}" style="{_BTN_STYLE};background:#e8f0fe;color:#1a56db;border:1px solid #c7d7fb">Open in {app_name}</a>',
    ]
    if oa_url:
        btns.append(
            f'<a href="{oa_url}" style="{_BTN_STYLE};background:#d9eee1;color:#1a7a42;border:1px solid #b4dec3">Full Text</a>'
        )
    if proxy_link:
        btns.append(
            f'<a href="{proxy_link}" style="{_BTN_STYLE};background:#f1f3f5;color:#495057;border:1px solid #dee2e6">Full Text via Proxy</a>'
        )

    card_divs = (
        f'<div style="margin-bottom:8px">{badges_html}</div>'
        f'<h3 style="margin:0 0 5px;font-size:1em;font-weight:600;line-height:1.4">'
        f'<a href="{paper_url}" style="color:#212529;text-decoration:none">{title}</a></h3>'
        f'<p style="margin:0 0 3px;color:#6c757d;font-size:.875em">{authors_str}</p>'
        f'<p style="margin:0 0 14px;color:#6c757d;font-size:.875em">{meta_html}</p>'
        f'<div>{"".join(btns)}</div>'
    )
    return (
        '\n<div style="border:1px solid #e9ecef;border-radius:8px;'
        'padding:18px 20px;margin-bottom:12px;background:#ffffff">'
        f'{card_divs}</div>'
    )


def build_html_digest(papers: list, mesh_topic_map: dict, mesh_topic_colours: dict,
                      base_url: str, username: str, today_str: str,
                      config: dict | None = None, group_by_profile: bool = False) -> str:
    app_name = (config or {}).get("app_name") or "MedPapers Daily"
    paper_word = "paper" if len(papers) == 1 else "papers"
    cfg = config or {}
    _sentinel = object()
    rows_html = ""
    if group_by_profile:
        current_profile = _sentinel
        for p in papers:
            prof = p.get("search_profile") or ""
            if prof != current_profile:
                current_profile = prof
                top_m = "margin-top:24px;" if current_profile is not _sentinel else ""
                rows_html += (
                    f'<div style="{top_m}margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #dee2e6">'
                    f'<p style="margin:0;font-size:.75em;font-weight:700;color:#6c757d;'
                    f'text-transform:uppercase;letter-spacing:.06em">{prof or "Other"}</p></div>'
                )
            rows_html += _render_paper_card(p, mesh_topic_map, mesh_topic_colours, base_url, cfg, app_name)
    else:
        for p in papers:
            rows_html += _render_paper_card(p, mesh_topic_map, mesh_topic_colours, base_url, cfg, app_name)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;max-width:660px;margin:0 auto;padding:24px 16px;color:#212529;background:#f8f9fa">
  <div style="margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid #e9ecef">
    <p style="margin:0 0 3px;font-size:1.2em;font-weight:700;color:#212529">{app_name}</p>
    <p style="margin:0;color:#6c757d;font-size:.9em">{today_str} &mdash; {len(papers)} new {paper_word}</p>
  </div>
  {rows_html}
  <div style="margin-top:20px;padding-top:14px;border-top:1px solid #e9ecef;text-align:center">
    <p style="margin:0;color:#adb5bd;font-size:.8em">
      <a href="{base_url}" style="color:#adb5bd;text-decoration:none">{app_name}</a> &bull;
      <a href="{base_url}/settings" style="color:#adb5bd;text-decoration:none">Manage preferences</a>
    </p>
  </div>
</body>
</html>"""


def build_plain_digest(papers: list, base_url: str, app_name: str = "MedPapers Daily") -> str:
    lines = [f"{app_name} Digest — {len(papers)} new paper(s)\n", "=" * 60]
    for p in papers:
        lines.append(f"\n{p['title']}")
        authors = json.loads(p["authors"] or "[]")
        if authors:
            lines.append(f"  {authors[0]}" + (f" et al." if len(authors) > 1 else ""))
        lines.append(f"  {_clean_journal(p['journal'])} | {p['pub_date'] or ''}")
        lines.append(f"  PubMed: https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}")
        lines.append(f"  {app_name}: {base_url}/paper/{p['pmid']}")
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
    app_name = config.get("app_name", "MedPapers Daily")
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
    app_name = config.get("app_name", "MedPapers Daily")

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
        email_only_new = user_cfg.get("email_only_new", True)
        mesh_topic_map = user_cfg.get("mesh_topic_map", {})
        mesh_topic_colours = user_cfg.get("mesh_topic_colours", {})
        group_by_profile = user_cfg.get("email_group_by_profile", False)
        user_id = None
        display_name = username

        try:
            with conn_ctx(db_path) as conn:
                row = conn.execute(
                    "SELECT id, display_name FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    print(f"[email] User {username} not in DB, skipping.")
                    continue
                user_id = row["id"]
                display_name = row["display_name"] or username

                order_sql = (
                    "sp.name NULLS LAST, up.added_at DESC"
                    if group_by_profile else "up.added_at DESC"
                )
                if email_only_new:
                    extra_filter = "AND up.emailed_at IS NULL"
                    query_params = (user_id,)
                else:
                    extra_filter = "AND up.added_at >= ?"
                    query_params = (user_id, cutoff)
                papers_rows = conn.execute(
                    f"""SELECT p.*, sp.name as search_profile FROM papers p
                       JOIN user_papers up ON up.pmid = p.pmid
                       LEFT JOIN search_profiles sp ON sp.id = up.search_profile_id
                       WHERE up.user_id = ?
                         AND up.is_read = 0
                         AND up.is_starred = 0
                         AND up.folder_id IS NULL
                         {extra_filter}
                       ORDER BY {order_sql}""",
                    query_params,
                ).fetchall()

            papers = [dict(r) for r in papers_rows]

            if not papers:
                if suppress_empty:
                    print(f"[email] {username}: no new papers, suppressing email.")
                    continue
                # Send nothing-new confirmation
                nothing_subject = f"[{app_name}] No new papers today, {today_str}"
                nothing_body = (
                    f"{app_name} ran successfully on {today_str} "
                    f"but found no new unactioned papers for {username}.\n\n"
                    f"Visit {base_url}/feed to review your paper feed.\n"
                )
                send_email(config, user_email, nothing_subject,
                           f"<p>{nothing_body.replace(chr(10), '<br>')}</p>",
                           nothing_body)
                _log_mail(db_path, user_id, user_email, nothing_subject, "sent")
                print(f"[email] {username}: sent nothing-new notification.")
                continue

            html = build_html_digest(papers, mesh_topic_map, mesh_topic_colours, base_url, username, today_str, config, group_by_profile)
            plain = build_plain_digest(papers, base_url, app_name)
            try:
                subject = subject_template.format(
                    new_papers=len(papers),
                    date=today_str,
                    name=display_name,
                    username=username,
                    display_name=display_name,
                    app_name=app_name,
                    s="s" if len(papers) != 1 else "",
                )
            except KeyError:
                subject = f"{len(papers)} new paper{'s' if len(papers) != 1 else ''} in your digest ({today_str})"
            send_email(config, user_email, subject, html, plain)
            _log_mail(db_path, user_id, user_email, subject, "sent")
            pmids = [p["pmid"] for p in papers]
            with conn_ctx(db_path) as conn:
                placeholders = ",".join("?" * len(pmids))
                conn.execute(
                    f"UPDATE user_papers SET emailed_at = ? WHERE user_id = ? AND pmid IN ({placeholders})",
                    [today.isoformat(), user_id] + pmids,
                )
            print(f"[email] {username}: sent digest with {len(papers)} paper(s).")

        except Exception:
            err = traceback.format_exc()
            print(f"[email] ERROR for {username}:\n{err}", file=sys.stderr)
            _log_mail(db_path, user_id, user_email, "digest", "error", err[:2000])
            if user_email:
                send_error_email(
                    config, user_email, "Email digest error",
                    f"{app_name} email digest failed for {username}:\n\n{err}",
                )


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()
