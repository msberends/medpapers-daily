import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.llm import DEFAULT_HIGHLIGHTS_PROMPT

from app.routes.settings import _enrich_profiles, _sync_profiles_to_db

BASE_DIR = Path(__file__).parent.parent.parent  # /var/www/papersdaily

import yaml
from fastapi import APIRouter, Request
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
    with open(scopus_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                q_num = int(float(row.get("quartile") or ""))
                q = f"Q{q_num}" if 1 <= q_num <= 4 else None
            except (ValueError, TypeError):
                q = None
            if q:
                quartile_counts[q] = quartile_counts.get(q, 0) + 1
            total += 1
            if len(preview) < 50:
                try:
                    cs = f"{float(row.get('citescore') or 0):.1f}"
                except (ValueError, TypeError):
                    cs = "—"
                preview.append({
                    "title": (row.get("title") or "").strip(),
                    "issn": (row.get("issn") or row.get("eIssn") or "").strip(),
                    "quartile": q or "—",
                    "citescore": cs,
                    "publisher": (row.get("publisher") or "").strip(),
                })
    return {"mtime": mtime, "total": total, "quartile_counts": quartile_counts, "preview": preview}


def _load_llm_status(base_dir: Path) -> dict:
    status_path = base_dir / "data" / "llm_status.json"
    if not status_path.exists():
        return {"status": "idle"}
    try:
        return json.loads(status_path.read_text())
    except Exception:
        return {"status": "idle"}


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    saved: str = "",
    error: str = "",
    test_sent: str = "",
    fetch_started: str = "",
    digest_started: str = "",
    scopus_started: str = "",
    recategorised: str = "",
    deleted: str = "",
    llm_started: str = "",
):
    require_admin(request)
    user = request.app.state.get_current_user(request)
    config = request.app.state.config
    scopus_path = BASE_DIR / "data" / "scopus_journals.csv"
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
        llm_pending = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE highlights IS NULL AND abstract IS NOT NULL AND abstract != ''"
        ).fetchone()[0]

    llm_status = _load_llm_status(BASE_DIR)

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
        "scopus_started": scopus_started,
        "recategorised": recategorised,
        "deleted": deleted,
        "llm_started": llm_started,
        "elsevier_api_key": config.get("elsevier_api_key", ""),
        "valid_themes": VALID_THEMES,
        "config": config,
        "scopus_stats": scopus_stats,
        "llm_pending": llm_pending,
        "llm_status": llm_status,
        "llm_has_api_key": bool(config.get("llm_api_key", "")),
        "llm_default_prompt": DEFAULT_HIGHLIGHTS_PROMPT,
    })


@router.post("/admin/save-config")
async def save_config(request: Request):
    require_admin(request)
    form = await request.form()
    existing = request.app.state.config

    relay = form.get("email_relay", "sendmail")

    new_config = {
        "app_name": form.get("app_name", "MedPapers Daily").strip(),
        "base_url": form.get("base_url", "").strip(),
        "port": _int(form.get("port"), 2711),
        "db_path": existing.get("db_path", "data/paperdigest.db"),
        "log_path": existing.get("log_path", "logs/fetch.log"),
        "elsevier_api_key": existing.get("elsevier_api_key", ""),
        "ncbi_api_key": form.get("ncbi_api_key", "").strip(),
        "ncbi_email": form.get("ncbi_email", "").strip(),
        "email_relay": relay,
        "email_from": form.get("email_from", "").strip(),
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
    app_name = config.get("app_name", "MedPapers Daily")
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



@router.post("/admin/save-scopus-key")
async def save_scopus_key(request: Request):
    require_admin(request)
    form = await request.form()
    config = request.app.state.config
    key = (form.get("elsevier_api_key") or "").strip()
    if key:
        config["elsevier_api_key"] = key
    with open(BASE_DIR / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    request.app.state.config = config
    return RedirectResponse("/admin?saved=1#tab-scopus", status_code=303)


@router.post("/admin/run-scopus-refresh")
async def run_scopus_refresh(request: Request):
    require_admin(request)
    venv_python = BASE_DIR / "venv" / "bin" / "python"
    refresh_script = BASE_DIR / "fetch_scopus_journals.py"
    log_path = BASE_DIR / "logs" / "scopus.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [str(venv_python), str(refresh_script)],
                stdout=log_f,
                stderr=log_f,
                cwd=str(BASE_DIR),
            )
        return RedirectResponse("/admin?scopus_started=1", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/admin?error={quote(str(e))}", status_code=303)


@router.get("/admin/scopus-refresh-status")
async def scopus_refresh_status(request: Request):
    require_admin(request)
    status_path = BASE_DIR / "data" / "scopus_refresh_status.json"
    if not status_path.exists():
        return {"status": "idle"}
    try:
        return json.loads(status_path.read_text())
    except Exception:
        return {"status": "idle"}


@router.post("/admin/reset-scopus-status")
async def reset_scopus_status(request: Request):
    require_admin(request)
    status_path = BASE_DIR / "data" / "scopus_refresh_status.json"
    try:
        status_path.unlink(missing_ok=True)
    except Exception:
        pass
    return RedirectResponse("/admin?saved=1#tab-scopus", status_code=303)


@router.post("/admin/run-recategorise")
async def run_recategorise(request: Request):
    require_admin(request)
    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    if not csv_path.exists():
        return RedirectResponse(
            "/admin?error=No+journal+data+yet.+Run+a+refresh+first.#tab-scopus",
            status_code=303,
        )
    mapping: dict[str, tuple] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                q_num = int(float(row.get("quartile") or ""))
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

    updated = 0
    with conn_ctx() as conn:
        papers = conn.execute(
            "SELECT pmid, issn FROM papers WHERE issn IS NOT NULL"
        ).fetchall()
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
    return RedirectResponse(f"/admin?recategorised={updated}#tab-scopus", status_code=303)


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
    if cfg.get("search_profiles"):
        cfg = {**cfg, "search_profiles": _enrich_profiles(target_user["id"], cfg["search_profiles"])}
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

    profile_ids = form.getlist("profile_id")
    profile_names = form.getlist("profile_name")
    profile_queries = form.getlist("profile_query")
    profiles = [
        {"name": n.strip(), "query": q.strip()}
        for n, q in zip(profile_names, profile_queries)
        if n.strip()
    ]
    with conn_ctx() as conn:
        target_row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
    if target_row:
        # enabled=1 for all admin-managed profiles (no toggle in admin form)
        enabled_all = ["1"] * len(profile_ids)
        _sync_profiles_to_db(target_row["id"], profile_ids, profile_names,
                             profile_queries, enabled_all)
        display_name = form.get("display_name", "").strip()
        with conn_ctx() as conn:
            conn.execute(
                "UPDATE users SET display_name=? WHERE id=?",
                (display_name or None, target_row["id"]),
            )

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
        "email_only_new": "email_only_new" in form,
        "fetch_enabled": "fetch_enabled" in form,
        "show_quartile": "show_quartile" in form,
        "q2_hard": "q2_hard" in form,
        "lookback_days": lookback_days,
        "show_export_ris": "show_export_ris" in form,
        "show_export_nbib": "show_export_nbib" in form,
        "abstract_style": form.get("abstract_style", "accent"),
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


@router.post("/admin/save-llm-config")
async def save_llm_config(request: Request):
    require_admin(request)
    form = await request.form()
    config = request.app.state.config

    provider = (form.get("llm_provider") or "").strip()
    new_api_key = (form.get("llm_api_key") or "").strip()
    model = (form.get("llm_model") or "").strip()
    ollama_url = (form.get("llm_ollama_url") or "").strip()
    prompt = (form.get("llm_prompt") or "").strip()
    allow_opt = "llm_allow_profile_optimisation" in form

    config["llm_provider"] = provider
    config["llm_model"] = model
    config["llm_ollama_url"] = ollama_url or "http://localhost:11434"
    config["llm_prompt"] = prompt
    config["llm_allow_profile_optimisation"] = allow_opt
    # Only overwrite the stored key when a non-empty value is submitted
    if new_api_key:
        config["llm_api_key"] = new_api_key

    with open(BASE_DIR / "config.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    request.app.state.config = config
    return RedirectResponse("/admin?saved=1#tab-llm", status_code=303)


@router.post("/admin/test-llm")
async def test_llm(request: Request):
    from fastapi.responses import JSONResponse
    require_admin(request)
    config = request.app.state.config
    if not config.get("llm_provider"):
        return JSONResponse({"ok": False, "error": "No LLM provider configured."})
    if not config.get("llm_model"):
        return JSONResponse({"ok": False, "error": "No model name configured."})
    from app.llm import call_llm
    try:
        response = call_llm(
            config,
            system_prompt="You are a helpful assistant.",
            user_message="Reply with exactly one word: OK",
            timeout=30,
        )
        return JSONResponse({"ok": True, "response": response.strip()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/admin/run-llm-highlights")
async def run_llm_highlights(request: Request):
    require_admin(request)
    config = request.app.state.config
    if not config.get("llm_provider"):
        return RedirectResponse(
            "/admin?error=No+LLM+provider+configured#tab-llm", status_code=303
        )
    venv_python = BASE_DIR / "venv" / "bin" / "python"
    llm_script = BASE_DIR / "llm_highlights.py"
    log_path = BASE_DIR / "logs" / "llm.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "ab") as log_f:
            subprocess.Popen(
                [str(venv_python), "-u", str(llm_script), "--force"],
                stdout=log_f,
                stderr=log_f,
                cwd=str(BASE_DIR),
            )
        return RedirectResponse("/admin?llm_started=1#tab-llm", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/admin?error={quote(str(e))}#tab-llm", status_code=303)


@router.get("/admin/llm-status")
async def llm_status(request: Request):
    require_admin(request)
    data = _load_llm_status(BASE_DIR)
    with conn_ctx() as conn:
        data["pending"] = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE highlights IS NULL AND abstract IS NOT NULL AND abstract != ''"
        ).fetchone()[0]
    return data


@router.post("/admin/reset-llm-status")
async def reset_llm_status(request: Request):
    require_admin(request)
    status_path = BASE_DIR / "data" / "llm_status.json"
    try:
        status_path.unlink(missing_ok=True)
    except Exception:
        pass
    return RedirectResponse("/admin?saved=1#tab-llm", status_code=303)


def _int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default
