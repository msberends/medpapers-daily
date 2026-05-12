import csv
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).parent.parent.parent  # /var/www/papersdaily

import yaml
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_admin, hash_password
from app.db import conn_ctx
from app.themes import VALID_THEMES

router = APIRouter()


def _load_user_cfg(username: str) -> dict:
    path = BASE_DIR / "users" / f"{username}.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_user_cfg(username: str, data: dict):
    path = BASE_DIR / "users" / f"{username}.yaml"
    path.parent.mkdir(exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _load_scopus_stats(scopus_path: Path) -> dict:
    if not scopus_path.exists():
        return {"mtime": None, "total": 0, "quartile_counts": {}, "preview": []}
    mtime = datetime.fromtimestamp(scopus_path.stat().st_mtime, tz=timezone.utc).isoformat()
    quartile_counts: dict[str, int] = {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
    preview: list[dict] = []
    total = 0
    with open(scopus_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            q = (row.get("SJR Best Quartile") or "").strip()
            if q in quartile_counts:
                quartile_counts[q] += 1
            total += 1
            if len(preview) < 50:
                def _fmt(raw: str) -> str:
                    raw = raw.strip()
                    try:
                        return f"{float(raw.replace(',', '.')):.2f}"
                    except (ValueError, AttributeError):
                        return raw or "—"
                preview.append({
                    "rank": row.get("Rank", "").strip(),
                    "title": row.get("Title", "").strip(),
                    "issn": row.get("Issn", "").strip(),
                    "quartile": q or "—",
                    "if_score": _fmt(row.get("Citations / Doc. (2years)", "")),
                    "citescore": _fmt(row.get("Citations / Doc. (3years)", "")),
                    "sjr": _fmt(row.get("SJR", "")),
                    "publisher": row.get("Publisher", "").strip(),
                })
    return {"mtime": mtime, "total": total, "quartile_counts": quartile_counts, "preview": preview}


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    saved: str = "",
    error: str = "",
    test_sent: str = "",
    fetch_started: str = "",
    digest_started: str = "",
    deleted: str = "",
):
    require_admin(request)
    user = request.app.state.get_current_user(request)
    config = request.app.state.config
    scopus_path = BASE_DIR / config.get("scopus_file", "data/scopus.csv")
    scopus_stats = _load_scopus_stats(scopus_path)

    with conn_ctx() as conn:
        users = conn.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY username"
        ).fetchall()
        user_emails = {
            u["username"]: _load_user_cfg(u["username"]).get("email", "")
            for u in users
        }
        paper_counts = {
            row["user_id"]: row["cnt"]
            for row in conn.execute(
                "SELECT user_id, COUNT(*) AS cnt FROM user_papers GROUP BY user_id"
            ).fetchall()
        }
        papers = conn.execute(
            """SELECT p.pmid, p.title, p.journal, p.publisher, p.pub_date, p.epub_date,
                      p.scopus_quartile, p.first_seen_at,
                      GROUP_CONCAT(u.username, ', ') AS usernames
               FROM papers p
               LEFT JOIN user_papers up ON up.pmid = p.pmid
               LEFT JOIN users u ON u.id = up.user_id
               GROUP BY p.pmid
               ORDER BY p.first_seen_at DESC"""
        ).fetchall()
        all_publishers = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT publisher FROM papers WHERE publisher IS NOT NULL AND publisher != '' ORDER BY publisher"
            ).fetchall()
        ]

    return request.app.state.templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": [dict(u) for u in users],
        "user_emails": user_emails,
        "paper_counts": paper_counts,
        "papers": [dict(p) for p in papers],
        "all_publishers": all_publishers,
        "saved": saved,
        "error": error,
        "test_sent": test_sent,
        "fetch_started": fetch_started,
        "digest_started": digest_started,
        "deleted": deleted,
        "valid_themes": VALID_THEMES,
        "config": config,
        "scopus_stats": scopus_stats,
    })


@router.post("/admin/save-config")
async def save_config(request: Request):
    require_admin(request)
    form = await request.form()
    existing = request.app.state.config

    relay = form.get("email_relay", "sendmail")

    new_config = {
        "app_name": form.get("app_name", "Papers Daily").strip(),
        "base_url": form.get("base_url", "").strip(),
        "port": _int(form.get("port"), 2711),
        "db_path": existing.get("db_path", "data/paperdigest.db"),
        "scopus_file": existing.get("scopus_file", "data/scopus.csv"),
        "log_path": existing.get("log_path", "logs/fetch.log"),
        "ncbi_api_key": form.get("ncbi_api_key", "").strip(),
        "ncbi_email": form.get("ncbi_email", "").strip(),
        "email_relay": relay,
        "email_from": form.get("email_from", "").strip(),
        "email_from_name": form.get("email_from_name", "").strip(),
        "email_subject_template": form.get("email_subject_template", "Your daily digest, {new_papers} new paper(s), {date}").strip(),
        "bootstrap_theme": form.get("bootstrap_theme", "flatly"),
        "theme": "light",
        "timezone": form.get("timezone", "UTC").strip(),
        "proxy_enabled": "proxy_enabled" in form,
        "proxy_domain": form.get("proxy_domain", "").strip(),
        "proxy_url_regex": form.get("proxy_url_regex", "").strip(),
    }

    if relay == "smtp":
        new_config["smtp_host"] = form.get("smtp_host", "smtp.gmail.com").strip()
        new_config["smtp_port"] = _int(form.get("smtp_port"), 587)
        new_smtp_user = form.get("smtp_user", "").strip()
        new_config["smtp_user"] = new_smtp_user if new_smtp_user else existing.get("smtp_user", "")
        new_pw = form.get("smtp_password", "").strip()
        new_config["smtp_password"] = new_pw if new_pw else existing.get("smtp_password", "")

    if existing.get("publisher_map"):
        new_config["publisher_map"] = existing["publisher_map"]
    if existing.get("predatory_publishers"):
        new_config["predatory_publishers"] = existing["predatory_publishers"]

    with open(BASE_DIR / "config.yaml", "w") as f:
        yaml.dump(new_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    request.app.state.config = new_config
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.post("/admin/test-email")
async def test_email(request: Request):
    require_admin(request)
    user = request.app.state.get_current_user(request)
    config = request.app.state.config
    cfg = _load_user_cfg(user["username"])
    to = cfg.get("email", "")
    if not to:
        return RedirectResponse("/admin?error=No+email+address+configured+for+your+account", status_code=303)
    from urllib.parse import quote
    app_name = config.get("app_name", "Papers Daily")
    subject = f"[{app_name}] Test email"
    now = datetime.now(timezone.utc).isoformat()
    try:
        from mail_helper import send_email as _send
        html = (
            f"<p>This is a test email from <strong>{app_name}</strong>.</p>"
            "<p>If you received this, your email settings are configured correctly.</p>"
        )
        plain = f"This is a test email from {app_name}.\nIf you received this, your email settings are configured correctly."
        _send(config, to, subject, html, plain)
        with conn_ctx() as conn:
            conn.execute(
                """INSERT INTO mail_log (user_id, sent_at, to_addr, subject, status)
                   VALUES (?,?,?,?,?)""",
                (user["user_id"], now, to, subject, "sent"),
            )
    except Exception as e:
        with conn_ctx() as conn:
            conn.execute(
                """INSERT INTO mail_log (user_id, sent_at, to_addr, subject, status, error)
                   VALUES (?,?,?,?,?,?)""",
                (user["user_id"], now, to, subject, "error", str(e)),
            )
        return RedirectResponse(f"/admin?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/admin?test_sent={quote(to)}", status_code=303)


@router.post("/admin/upload-scopus")
async def upload_scopus(request: Request, scopus_file: UploadFile = File(...)):
    require_admin(request)
    config = request.app.state.config
    dest = BASE_DIR / config.get("scopus_file", "data/scopus.csv")
    dest.parent.mkdir(exist_ok=True)
    content = await scopus_file.read()
    with open(dest, "wb") as f:
        f.write(content)
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.post("/admin/create-user")
async def create_user(request: Request):
    require_admin(request)
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    email = (form.get("email") or "").strip()
    password = (form.get("password") or "").strip()
    is_admin = 1 if form.get("is_admin") else 0

    if not username or not password:
        return RedirectResponse("/admin?error=Username+and+password+required", status_code=303)
    now = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        if conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            return RedirectResponse(f"/admin?error=User+{username}+already+exists", status_code=303)
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (username, hash_password(password), is_admin, now),
        )
    if not (BASE_DIR / "users" / f"{username}.yaml").exists():
        _save_user_cfg(username, {
            "email": email,
            "email_suppress_empty": True,
            "fetch_enabled": True,
            "show_quartile": True,
            "q2_hard": True,
            "search_profiles": [],
            "mesh_topic_map": {},
            "folders": [],
        })
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.post("/admin/delete-user/{user_id}")
async def delete_user(user_id: int, request: Request):
    require_admin(request)
    current = request.app.state.get_current_user(request)
    if current and current["user_id"] == user_id:
        return RedirectResponse("/admin?error=Cannot+delete+yourself", status_code=303)
    with conn_ctx() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return RedirectResponse("/admin?saved=1", status_code=303)


@router.get("/admin/users/{username}", response_class=HTMLResponse)
async def admin_user_page(username: str, request: Request, saved: str = "", error: str = ""):
    require_admin(request)
    user = request.app.state.get_current_user(request)
    cfg = _load_user_cfg(username)
    with conn_ctx() as conn:
        target_user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not target_user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")
    return request.app.state.templates.TemplateResponse(request, "admin_user.html", {
        "user": user,
        "target_user": dict(target_user),
        "cfg": cfg,
        "saved": saved,
        "error": error,
        "config": request.app.state.config,
    })


@router.post("/admin/users/{username}/save")
async def admin_save_user(username: str, request: Request):
    require_admin(request)
    form = await request.form()
    existing = _load_user_cfg(username)

    profile_names = form.getlist("profile_name")
    profile_queries = form.getlist("profile_query")
    profiles = [
        {"name": n.strip(), "query": q.strip()}
        for n, q in zip(profile_names, profile_queries)
        if n.strip()
    ]

    mesh_terms = form.getlist("mesh_term")
    mesh_topics = form.getlist("mesh_topic")
    mesh_map = {}
    for term, topic in zip(mesh_terms, mesh_topics):
        if term.strip():
            mesh_map[term.strip()] = topic.strip()

    try:
        lookback_days = max(1, min(90, int(form.get("lookback_days", 7))))
    except (ValueError, TypeError):
        lookback_days = 7

    data = {
        **existing,
        "email": (form.get("email") or "").strip(),
        "email_suppress_empty": "email_suppress_empty" in form,
        "fetch_enabled": "fetch_enabled" in form,
        "show_quartile": "show_quartile" in form,
        "q2_hard": "q2_hard" in form,
        "lookback_days": lookback_days,
        "search_profiles": profiles,
        "mesh_topic_map": mesh_map,
        "folders": existing.get("folders", []),
    }
    _save_user_cfg(username, data)
    return RedirectResponse(f"/admin/users/{username}?saved=1", status_code=303)


@router.post("/admin/users/{user_id}/toggle-admin")
async def toggle_admin(user_id: int, request: Request):
    require_admin(request)
    with conn_ctx() as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET is_admin = ? WHERE id = ?",
                (0 if row["is_admin"] else 1, user_id),
            )
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/run-fetch")
async def run_fetch(request: Request):
    require_admin(request)
    venv_python = BASE_DIR / "venv" / "bin" / "python"
    fetch_script = BASE_DIR / "fetch.py"
    log_path = BASE_DIR / "logs" / "fetch.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [str(venv_python), str(fetch_script)],
                stdout=log_f,
                stderr=log_f,
                cwd=str(BASE_DIR),
            )
        return RedirectResponse("/admin?fetch_started=1", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/admin?error={quote(str(e))}", status_code=303)


@router.post("/admin/run-digest")
async def run_digest(request: Request):
    require_admin(request)
    venv_python = BASE_DIR / "venv" / "bin" / "python"
    digest_script = BASE_DIR / "email_digest.py"
    log_path = BASE_DIR / "logs" / "email.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [str(venv_python), str(digest_script)],
                stdout=log_f,
                stderr=log_f,
                cwd=str(BASE_DIR),
            )
        return RedirectResponse("/admin?digest_started=1", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/admin?error={quote(str(e))}", status_code=303)


@router.post("/admin/delete-papers")
async def delete_papers(request: Request):
    require_admin(request)
    form = await request.form()
    pmid_list = form.getlist("pmid")
    if not pmid_list:
        return RedirectResponse("/admin?error=No+papers+selected&deleted=0", status_code=303)
    placeholders = ",".join("?" * len(pmid_list))
    with conn_ctx() as conn:
        conn.execute(f"DELETE FROM user_papers WHERE pmid IN ({placeholders})", pmid_list)
        conn.execute(f"DELETE FROM papers WHERE pmid IN ({placeholders})", pmid_list)
    return RedirectResponse(f"/admin?deleted={len(pmid_list)}", status_code=303)


@router.post("/admin/save-publisher-map")
async def save_publisher_map(request: Request):
    require_admin(request)
    form = await request.form()
    keys = form.getlist("pub_key")
    vals = form.getlist("pub_val")
    mapping = {k: v.strip() for k, v in zip(keys, vals) if k and v.strip()}
    predatory = form.getlist("pub_predatory")
    config = request.app.state.config
    config["publisher_map"] = mapping
    config["predatory_publishers"] = predatory
    with open(BASE_DIR / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return RedirectResponse("/admin?saved=1#tab-publishers", status_code=303)


def _int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
