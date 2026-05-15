from datetime import datetime, timezone

import json

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

from app.auth import require_auth, verify_password, hash_password
from app.db import conn_ctx

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent  # /var/www/papersdaily


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


def _get_relevance_cfg(user_yaml: dict) -> dict:
    return {
        "enabled": user_yaml.get("relevance_alert_enabled", True),
        "threshold": user_yaml.get("relevance_alert_threshold", 0.30),
        "min_rated": user_yaml.get("relevance_alert_min_rated", 10),
        "lookback_days": user_yaml.get("relevance_alert_lookback_days", 30),
    }


def _enrich_profiles(user_id: int, profiles: list[dict]) -> list[dict]:
    """Add 'id' field to each profile dict by looking up the search_profiles table."""
    if not profiles:
        return profiles
    with conn_ctx() as conn:
        db_map = {
            r["name"]: r["id"]
            for r in conn.execute(
                "SELECT id, name FROM search_profiles WHERE user_id = ?", (user_id,)
            ).fetchall()
        }
    return [{**p, "id": db_map.get(p.get("name", ""), "")} for p in profiles]


def _sync_profiles_to_db(user_id: int, profile_ids: list[str],
                         profile_names: list[str], profile_queries: list[str],
                         profile_enableds: list[str]):
    """Sync the submitted profile form fields to the search_profiles table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    form_profile_ids: set[int] = set()
    with conn_ctx() as conn:
        for pid, name, query, enabled in zip(profile_ids, profile_names,
                                             profile_queries, profile_enableds):
            name = name.strip()
            if not name:
                continue
            enabled_int = 1 if enabled == "1" else 0
            if pid and pid.strip().isdigit():
                pid_int = int(pid.strip())
                conn.execute(
                    "UPDATE search_profiles SET name=?, query=?, enabled=? WHERE id=? AND user_id=?",
                    (name, query.strip(), enabled_int, pid_int, user_id),
                )
                form_profile_ids.add(pid_int)
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO search_profiles
                       (user_id, name, query, enabled, created_at) VALUES (?,?,?,?,?)""",
                    (user_id, name, query.strip(), enabled_int, now_iso),
                )
                new_row = conn.execute(
                    "SELECT id FROM search_profiles WHERE user_id=? AND name=?",
                    (user_id, name),
                ).fetchone()
                if new_row:
                    form_profile_ids.add(new_row["id"])
        # Delete DB entries that were removed from the form
        all_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM search_profiles WHERE user_id=?", (user_id,)
        ).fetchall()]
        for old_id in all_ids:
            if old_id not in form_profile_ids:
                conn.execute("DELETE FROM search_profiles WHERE id=?", (old_id,))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = "", error: str = "", tab: str = ""):
    user = require_auth(request)
    cfg = _load_user_cfg(user["username"])
    if cfg.get("search_profiles"):
        cfg = {**cfg, "search_profiles": _enrich_profiles(user["user_id"], cfg["search_profiles"])}

    admin_config = request.app.state.config
    rel_cfg = _get_relevance_cfg(cfg)

    # Per-profile relevance stats for the settings Relevance tab
    profile_relevance_stats: dict = {}
    date_modifier = f"-{int(rel_cfg['lookback_days'])} days"
    with conn_ctx() as conn:
        stat_rows = conn.execute(
            """SELECT sp.id,
                      SUM(CASE WHEN up.relevance = 1  THEN 1 ELSE 0 END) AS relevant,
                      SUM(CASE WHEN up.relevance = -1 THEN 1 ELSE 0 END) AS not_relevant,
                      SUM(CASE WHEN up.relevance IS NOT NULL THEN 1 ELSE 0 END) AS rated
               FROM search_profiles sp
               LEFT JOIN user_paper_profiles upp
                   ON upp.profile_id = sp.id AND upp.user_id = sp.user_id
                   AND date(upp.added_at) >= date('now', ?)
               LEFT JOIN user_papers up
                   ON up.user_id = upp.user_id AND up.pmid = upp.pmid
               WHERE sp.user_id = ?
               GROUP BY sp.id""",
            (date_modifier, user["user_id"]),
        ).fetchall()
        for r in stat_rows:
            profile_relevance_stats[r["id"]] = {
                "relevant": r["relevant"] or 0,
                "not_relevant": r["not_relevant"] or 0,
                "rated": r["rated"] or 0,
            }

    # Collect all unique MeSH terms and author keywords from this user's papers
    all_mesh: set[str] = set()
    all_keywords: set[str] = set()
    with conn_ctx() as conn:
        term_rows = conn.execute(
            """SELECT p.mesh_terms, p.keywords
               FROM papers p
               JOIN user_papers up ON up.pmid = p.pmid
               WHERE up.user_id = ?""",
            (user["user_id"],),
        ).fetchall()
    for row in term_rows:
        all_mesh.update(t.lower() for t in json.loads(row["mesh_terms"] or "[]") if t)
        all_keywords.update(t.lower() for t in json.loads(row["keywords"] or "[]") if t)

    normalized_topic_map = {k.lower(): v for k, v in cfg.get("mesh_topic_map", {}).items()}

    topics_grouped: dict[str, list[str]] = {}
    for term, topic in cfg.get("mesh_topic_map", {}).items():
        topics_grouped.setdefault(topic, []).append(term)
    all_terms_combined = sorted(all_mesh | all_keywords)
    mesh_topic_colours = cfg.get("mesh_topic_colours", {})
    _colour_cycle = ["blue","purple","green","orange","teal","red","indigo","yellow","pink","cyan","primary","success","danger","warning","info","secondary"]
    effective_topic_colours = {
        name: mesh_topic_colours.get(name, _colour_cycle[i % len(_colour_cycle)])
        for i, name in enumerate(topics_grouped)
    }

    return request.app.state.templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "cfg": cfg,
        "saved": saved,
        "error": error,
        "tab": tab,
        "config": admin_config,
        "rel_cfg": rel_cfg,
        "profile_relevance_stats": profile_relevance_stats,
        "all_mesh_terms": sorted(all_mesh),
        "all_keywords": sorted(all_keywords),
        "normalized_topic_map": normalized_topic_map,
        "topics_grouped": topics_grouped,
        "all_terms_combined": all_terms_combined,
        "effective_topic_colours": effective_topic_colours,
    })


@router.post("/settings/save")
async def save_settings(request: Request):
    user = require_auth(request)
    form = await request.form()

    existing = _load_user_cfg(user["username"])

    display_name = form.get("display_name", "").strip()
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE users SET display_name=? WHERE id=?",
            (display_name or None, user["user_id"]),
        )

    profile_ids = form.getlist("profile_id")
    profile_names = form.getlist("profile_name")
    profile_queries = form.getlist("profile_query")
    profile_enableds = form.getlist("profile_enabled")
    profiles = [
        {"name": n.strip(), "query": q.strip(), "enabled": e == "1"}
        for n, q, e in zip(profile_names, profile_queries, profile_enableds)
        if n.strip()
    ]
    _sync_profiles_to_db(user["user_id"], profile_ids, profile_names,
                         profile_queries, profile_enableds)

    mesh_terms = form.getlist("mesh_term")
    mesh_topics = form.getlist("mesh_topic")
    mesh_map = {}
    for term, topic in zip(mesh_terms, mesh_topics):
        if term.strip():
            mesh_map[term.strip()] = topic.strip()

    from app.themes import VALID_THEMES
    theme = form.get("bootstrap_theme", "").strip()

    fetch_schedule = form.get("fetch_schedule", "daily").strip()
    if fetch_schedule not in ("daily", "weekly", "monthly"):
        fetch_schedule = "daily"
    try:
        fetch_schedule_dow = max(0, min(6, int(form.get("fetch_schedule_dow", 0))))
    except (ValueError, TypeError):
        fetch_schedule_dow = 0
    try:
        fetch_schedule_dom = max(1, min(28, int(form.get("fetch_schedule_dom", 1))))
    except (ValueError, TypeError):
        fetch_schedule_dom = 1
    try:
        page_size = int(form.get("page_size", 50))
        if page_size not in (10, 25, 50, 100, 150, 200, 500):
            page_size = 50
    except (ValueError, TypeError):
        page_size = 50
    try:
        lookback_days = max(1, min(90, int(form.get("lookback_days", 7))))
    except (ValueError, TypeError):
        lookback_days = 7

    data = {
        **existing,
        "email": form.get("email", "").strip(),
        "email_suppress_empty": "email_suppress_empty" in form,
        "email_only_new": "email_only_new" in form,
        "email_group_by_profile": "email_group_by_profile" in form,
        "fetch_enabled": "fetch_enabled" in form,
        "fetch_schedule": fetch_schedule,
        "fetch_schedule_dow": fetch_schedule_dow,
        "fetch_schedule_dom": fetch_schedule_dom,
        "page_size": page_size,
        "lookback_days": lookback_days,
        "show_quartile": "show_quartile" in form,
        "feed_group_by_profile": "feed_group_by_profile" in form,
        "q2_hard": "q2_hard" in form,
        "bootstrap_theme": theme if (theme == "" or theme in VALID_THEMES) else existing.get("bootstrap_theme", ""),
        "search_profiles": profiles,
        "mesh_topic_map": mesh_map,
        "folders": existing.get("folders", []),
    }

    # Relevance alert settings — only written when submitted from the Relevance tab
    if "relevance_tab" in form:
        data["relevance_alert_enabled"] = "relevance_alert_enabled" in form
        try:
            t = max(1, min(99, int(form.get("relevance_alert_threshold", "30"))))
            data["relevance_alert_threshold"] = round(t / 100.0, 4)
        except (ValueError, TypeError):
            data["relevance_alert_threshold"] = existing.get("relevance_alert_threshold", 0.30)
        try:
            data["relevance_alert_min_rated"] = max(1, min(1000, int(form.get("relevance_alert_min_rated", "10"))))
        except (ValueError, TypeError):
            data["relevance_alert_min_rated"] = existing.get("relevance_alert_min_rated", 10)
        try:
            data["relevance_alert_lookback_days"] = max(7, min(365, int(form.get("relevance_alert_lookback_days", "30"))))
        except (ValueError, TypeError):
            data["relevance_alert_lookback_days"] = existing.get("relevance_alert_lookback_days", 30)

    if "topics_tab" in form:
        try:
            data["mesh_topic_colours"] = json.loads(form.get("mesh_topic_colours_json", "{}"))
        except (ValueError, TypeError):
            data["mesh_topic_colours"] = existing.get("mesh_topic_colours", {})

    _save_user_cfg(user["username"], data)
    if "topics_tab" in form:
        redirect_tab = "mesh"
    elif "relevance_tab" in form:
        redirect_tab = "relevance"
    else:
        redirect_tab = ""
    redirect_url = f"/settings?saved=1&tab={redirect_tab}" if redirect_tab else "/settings?saved=1"
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/settings/reset-profile-relevance/{profile_id}")
async def reset_profile_relevance(profile_id: int, request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id FROM search_profiles WHERE id = ? AND user_id = ?",
            (profile_id, user["user_id"]),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE user_papers SET relevance = NULL WHERE user_id = ? AND search_profile_id = ?",
                (user["user_id"], profile_id),
            )
    return RedirectResponse("/settings?saved=1&tab=relevance", status_code=303)


@router.post("/settings/reset-all-relevance")
async def reset_all_relevance(request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET relevance = NULL WHERE user_id = ?",
            (user["user_id"],),
        )
    return RedirectResponse("/settings?saved=1&tab=relevance", status_code=303)



@router.post("/settings/change-password")
async def change_password(request: Request):
    user = require_auth(request)
    form = await request.form()
    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["user_id"],)
        ).fetchone()
        if not verify_password(current_password, row["password_hash"]):
            return RedirectResponse("/settings?error=Wrong+current+password", status_code=303)
        if new_password != confirm_password:
            return RedirectResponse("/settings?error=Passwords+do+not+match", status_code=303)
        if len(new_password) < 8:
            return RedirectResponse("/settings?error=Password+must+be+at+least+8+characters", status_code=303)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user["user_id"]),
        )
    return RedirectResponse("/settings?saved=1", status_code=303)
