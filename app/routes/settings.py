from datetime import datetime, timezone

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pathlib import Path

from app.auth import (
    require_auth, verify_password, hash_password,
    invalidate_other_sessions, SESSION_COOKIE,
)
from app.db import conn_ctx
from app.flash import flash_redirect
from app.user_config import load_user_cfg as _load_user_cfg, save_user_cfg as _save_user_cfg
from app.utils import get_relevance_cfg as _get_relevance_cfg

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent  # /var/www/papersdaily


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
async def settings_page(request: Request, tab: str = ""):
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
    # Apply q2_hard filter so the unclassified count matches what the feed shows.
    all_mesh: set[str] = set()
    all_keywords: set[str] = set()
    q2_hard = cfg.get("q2_hard", True)
    quartile_filter = " AND p.scopus_quartile IN ('Q1', 'Q2')" if q2_hard else ""
    with conn_ctx() as conn:
        term_rows = conn.execute(
            f"""SELECT p.mesh_terms, p.keywords
               FROM papers p
               JOIN user_papers up ON up.pmid = p.pmid
               WHERE up.user_id = ?{quartile_filter}""",
            (user["user_id"],),
        ).fetchall()
    for row in term_rows:
        all_mesh.update(t.lower() for t in json.loads(row["mesh_terms"] or "[]") if t)
        all_keywords.update(t.lower() for t in json.loads(row["keywords"] or "[]") if t)

    normalized_topic_map = {k.lower(): v for k, v in cfg.get("mesh_topic_map", {}).items()}

    total_papers_count = len(term_rows)
    unclassified_count = 0
    for row in term_rows:
        row_terms = set(t.lower() for t in json.loads(row["mesh_terms"] or "[]") if t)
        row_terms.update(t.lower() for t in json.loads(row["keywords"] or "[]") if t)
        if not any(t in normalized_topic_map for t in row_terms):
            unclassified_count += 1

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

    llm_available = bool(
        admin_config.get("llm_provider") and admin_config.get("llm_allow_profile_optimisation")
    )
    llm_topic_available = bool(
        admin_config.get("llm_provider") and admin_config.get("llm_allow_topic_suggestions")
    )

    return request.app.state.templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "cfg": cfg,
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
        "llm_available": llm_available,
        "llm_topic_available": llm_topic_available,
        "unclassified_count": unclassified_count,
        "total_papers_count": total_papers_count,
    })


@router.post("/settings/save/account")
async def save_settings_account(request: Request):
    """Save Account tab: display name (DB) + email address (YAML)."""
    user = require_auth(request)
    form = await request.form()
    existing = _load_user_cfg(user["username"])

    display_name = form.get("display_name", "").strip()
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE users SET display_name=? WHERE id=?",
            (display_name or None, user["user_id"]),
        )

    data = {**existing, "email": form.get("email", "").strip()}
    _save_user_cfg(user["username"], data)
    return flash_redirect("/settings?tab=account", "Account settings saved.")


@router.post("/settings/save/appearance")
async def save_settings_appearance(request: Request):
    """Save Appearance tab settings."""
    user = require_auth(request)
    form = await request.form()
    existing = _load_user_cfg(user["username"])

    from app.themes import VALID_THEMES
    theme = form.get("bootstrap_theme", "").strip()
    try:
        page_size = int(form.get("page_size", 50))
        if page_size not in (10, 25, 50, 100, 150, 200, 500):
            page_size = 50
    except (ValueError, TypeError):
        page_size = 50

    data = {
        **existing,
        "bootstrap_theme": theme if (theme == "" or theme in VALID_THEMES) else existing.get("bootstrap_theme", ""),
        "theme_mode": form.get("theme_mode", "").strip() if form.get("theme_mode", "") in ("dark", "system") else "",
        "page_size": page_size,
        "feed_group_by_profile": "feed_group_by_profile" in form,
        "show_flags": "show_flags" in form,
        "show_flags_feed": "show_flags_feed" in form,
        "show_export_ris": "show_export_ris" in form,
        "show_export_nbib": "show_export_nbib" in form,
        "abstract_style": form.get("abstract_style", "accent") if form.get("abstract_style", "accent") in ("accent", "pill") else "accent",
        "author_list_style": form.get("author_list_style", "truncate") if form.get("author_list_style", "") in ("all", "truncate") else "truncate",
        "show_quartile": "show_quartile" in form,
    }
    _save_user_cfg(user["username"], data)
    return flash_redirect("/settings?tab=appearance", "Appearance settings saved.")


@router.post("/settings/save/fetch")
async def save_settings_fetch(request: Request):
    """Save Fetch & Email tab settings."""
    user = require_auth(request)
    form = await request.form()
    existing = _load_user_cfg(user["username"])

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
        lookback_days = max(1, min(90, int(form.get("lookback_days", 7))))
    except (ValueError, TypeError):
        lookback_days = 7

    data = {
        **existing,
        "fetch_enabled": "fetch_enabled" in form,
        "fetch_schedule": fetch_schedule,
        "fetch_schedule_dow": fetch_schedule_dow,
        "fetch_schedule_dom": fetch_schedule_dom,
        "lookback_days": lookback_days,
        "q2_hard": "q2_hard" in form,
        "email_suppress_empty": "email_suppress_empty" in form,
        "email_only_new": "email_only_new" in form,
        "email_group_by_profile": "email_group_by_profile" in form,
    }
    _save_user_cfg(user["username"], data)
    return flash_redirect("/settings?tab=fetch", "Fetch &amp; email settings saved.")


@router.post("/settings/save/profiles")
async def save_settings_profiles(request: Request):
    """Save Search Profiles tab (profiles + relevance alert settings) only."""
    user = require_auth(request)
    form = await request.form()
    existing = _load_user_cfg(user["username"])

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

    data = {**existing, "search_profiles": profiles}

    try:
        t = max(1, min(99, int(form.get("relevance_alert_threshold", "30"))))
        data["relevance_alert_threshold"] = round(t / 100.0, 4)
    except (ValueError, TypeError):
        pass
    try:
        data["relevance_alert_min_rated"] = max(1, min(1000, int(form.get("relevance_alert_min_rated", "10"))))
    except (ValueError, TypeError):
        pass
    try:
        data["relevance_alert_lookback_days"] = max(7, min(365, int(form.get("relevance_alert_lookback_days", "30"))))
    except (ValueError, TypeError):
        pass
    data["relevance_alert_enabled"] = "relevance_alert_enabled" in form

    _save_user_cfg(user["username"], data)
    return flash_redirect("/settings?tab=profiles", "Search profiles saved.")


@router.post("/settings/save/topics")
async def save_settings_topics(request: Request):
    """Save Topics tab (mesh_topic_map + colours) only."""
    user = require_auth(request)
    form = await request.form()
    existing = _load_user_cfg(user["username"])

    mesh_terms = form.getlist("mesh_term")
    mesh_topics_list = form.getlist("mesh_topic")
    mesh_map = {}
    for term, topic in zip(mesh_terms, mesh_topics_list):
        if term.strip():
            mesh_map[term.strip()] = topic.strip()

    try:
        colours = json.loads(form.get("mesh_topic_colours_json", "{}"))
    except (ValueError, TypeError):
        colours = existing.get("mesh_topic_colours", {})

    data = {**existing, "mesh_topic_map": mesh_map, "mesh_topic_colours": colours}
    _save_user_cfg(user["username"], data)
    return flash_redirect("/settings?tab=mesh", "Topics saved.")


@router.post("/settings/save")
async def save_settings_legacy(request: Request):
    """Legacy single-endpoint shim — routes to the appropriate split endpoint."""
    form = await request.form()
    if "topics_tab" in form:
        return await save_settings_topics(request)
    if "relevance_tab" in form:
        return await save_settings_profiles(request)
    return await save_settings_account(request)


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
    return flash_redirect("/settings?tab=profiles", "Relevance ratings reset for this profile.")


@router.post("/settings/reset-all-relevance")
async def reset_all_relevance(request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET relevance = NULL WHERE user_id = ?",
            (user["user_id"],),
        )
    return flash_redirect("/settings?tab=profiles", "All relevance ratings reset.")



def _build_prompt(profile_name: str, query: str,
                  relevant: list[dict], not_relevant: list[dict]) -> str:
    def _fmt(papers: list[dict]) -> str:
        if not papers:
            return "  (none)\n"
        lines = []
        for i, p in enumerate(papers, 1):
            mesh_str = ", ".join(p["mesh"]) if p["mesh"] else "no MeSH terms"
            lines.append(f"  {i}. {p['title']}\n     MeSH: {mesh_str}")
        return "\n".join(lines) + "\n"

    return (
        "You are a PubMed search query expert. I use an automated literature monitoring tool "
        "that fetches papers from PubMed using the search profile described below. "
        "I have rated some of the retrieved papers as relevant or not relevant. "
        "Based on the titles and MeSH terms of those papers, suggest specific "
        "improvements to my PubMed query that would retrieve more of the relevant papers "
        "and fewer of the irrelevant ones.\n\n"
        f"SEARCH PROFILE NAME: {profile_name}\n\n"
        f"CURRENT PUBMED QUERY:\n{query}\n\n"
        f"RELEVANT PAPERS ({len(relevant)}):\n{_fmt(relevant)}\n"
        f"NOT RELEVANT PAPERS ({len(not_relevant)}):\n{_fmt(not_relevant)}\n"
        "Respond with ONLY valid JSON — no prose, no markdown fences — in this exact format:\n"
        '{"query": "<revised PubMed query here>", "rationale": "<one sentence explaining the key change>"}'
    )


def _parse_llm_query_response(text: str) -> dict | None:
    """Extract {"query": ..., "rationale": ...} from LLM response, tolerating minor formatting issues."""
    import re as _re
    text = _re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("query"), str):
            return {"query": data["query"].strip(), "rationale": str(data.get("rationale", "")).strip()}
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: try to extract a bare query from any {"query": ...} fragment
    m = _re.search(r'"query"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        return {"query": m.group(1).strip(), "rationale": ""}
    return None


@router.post("/settings/run-profile-llm/{profile_id}")
async def run_profile_llm(profile_id: int, request: Request):
    user = require_auth(request)
    config = request.app.state.config
    if not (config.get("llm_provider") and config.get("llm_allow_profile_optimisation")):
        return JSONResponse({"error": "LLM not available for this feature."}, status_code=403)

    from app.llm import call_llm

    with conn_ctx() as conn:
        profile = conn.execute(
            "SELECT id, name, query FROM search_profiles WHERE id = ? AND user_id = ?",
            (profile_id, user["user_id"]),
        ).fetchone()
        if not profile:
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        rows = conn.execute(
            """SELECT p.title, p.mesh_terms, up.relevance
               FROM user_paper_profiles upp
               JOIN papers p ON p.pmid = upp.pmid
               JOIN user_papers up ON up.user_id = upp.user_id AND up.pmid = upp.pmid
               WHERE upp.profile_id = ? AND upp.user_id = ? AND up.relevance IS NOT NULL
               ORDER BY upp.added_at DESC LIMIT 100""",
            (profile_id, user["user_id"]),
        ).fetchall()
    relevant, not_relevant = [], []
    for row in rows:
        mesh = json.loads(row["mesh_terms"] or "[]")
        entry = {"title": row["title"], "mesh": mesh}
        (relevant if row["relevance"] == 1 else not_relevant).append(entry)
    prompt = _build_prompt(profile["name"], profile["query"], relevant, not_relevant)
    try:
        import asyncio
        response = await asyncio.to_thread(call_llm, config, "", prompt)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    parsed = _parse_llm_query_response(response)
    return JSONResponse({
        "response": response,
        "profile_name": profile["name"],
        "original_query": profile["query"],
        "parsed": parsed,
    })


@router.get("/settings/profile-prompt/{profile_id}")
async def profile_prompt(profile_id: int, request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        profile = conn.execute(
            "SELECT id, name, query FROM search_profiles WHERE id = ? AND user_id = ?",
            (profile_id, user["user_id"]),
        ).fetchone()
        if not profile:
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        rows = conn.execute(
            """SELECT p.title, p.mesh_terms, up.relevance
               FROM user_paper_profiles upp
               JOIN papers p ON p.pmid = upp.pmid
               JOIN user_papers up ON up.user_id = upp.user_id AND up.pmid = upp.pmid
               WHERE upp.profile_id = ? AND upp.user_id = ? AND up.relevance IS NOT NULL
               ORDER BY upp.added_at DESC LIMIT 100""",
            (profile_id, user["user_id"]),
        ).fetchall()
    relevant, not_relevant = [], []
    for row in rows:
        mesh = json.loads(row["mesh_terms"] or "[]")
        entry = {"title": row["title"], "mesh": mesh}
        (relevant if row["relevance"] == 1 else not_relevant).append(entry)
    prompt = _build_prompt(profile["name"], profile["query"], relevant, not_relevant)
    return JSONResponse({"prompt": prompt, "profile_name": profile["name"]})


def _build_topic_suggestions_prompt(topics: list[str], unassigned_terms: list[str]) -> str:
    topics_str = "\n".join(f"- {t}" for t in topics)
    terms_str = "\n".join(f"- {t}" for t in unassigned_terms)
    return (
        "Assign biomedical terms to topic categories.\n"
        "Return ONLY a JSON array — no prose, no markdown fences.\n\n"
        f"TOPICS:\n{topics_str}\n\n"
        f"TERMS (MeSH headings and author keywords):\n{terms_str}\n\n"
        "Rules:\n"
        "- Assign each term to at most one topic.\n"
        "- Only include confident assignments; omit uncertain ones.\n"
        "- Copy strings exactly from the lists above.\n\n"
        'Format: [{"term": "...", "topic": "..."}]\n'
        "If nothing is confident: []"
    )


def _parse_topic_suggestions(text: str, valid_topics: set[str],
                              valid_terms: set[str]) -> list[dict]:
    import re
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        term = (item.get("term") or "").strip().lower()
        topic = (item.get("topic") or "").strip()
        if not term or not topic:
            continue
        if term not in valid_terms:
            continue
        if topic not in valid_topics:
            continue
        if term in seen:
            continue
        seen.add(term)
        result.append({"term": term, "topic": topic})
    return result


@router.post("/settings/suggest-topic-terms")
async def suggest_topic_terms(request: Request):
    user = require_auth(request)
    config = request.app.state.config
    if not (config.get("llm_provider") and config.get("llm_allow_topic_suggestions")):
        return JSONResponse({"error": "LLM topic suggestions are not enabled."}, status_code=403)

    from app.llm import call_llm

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body."}, status_code=400)

    topics = [str(t).strip() for t in (body.get("topics") or []) if str(t).strip()]
    assigned_raw = [str(t).strip().lower() for t in (body.get("assigned_terms") or []) if str(t).strip()]
    assigned = set(assigned_raw)

    if not topics:
        return JSONResponse({"error": "No topics provided."}, status_code=400)

    from collections import Counter
    term_counts: Counter = Counter()
    with conn_ctx() as conn:
        term_rows = conn.execute(
            """SELECT p.mesh_terms, p.keywords
               FROM papers p
               JOIN user_papers up ON up.pmid = p.pmid
               WHERE up.user_id = ?""",
            (user["user_id"],),
        ).fetchall()
    for row in term_rows:
        for t in json.loads(row["mesh_terms"] or "[]"):
            if t:
                term_counts[t.lower()] += 1
        for t in json.loads(row["keywords"] or "[]"):
            if t:
                term_counts[t.lower()] += 1

    # Sort by prevalence (most papers first), then alphabetically; exclude already-assigned
    unassigned = sorted(
        (t for t in term_counts if t not in assigned),
        key=lambda t: (-term_counts[t], t),
    )[:80]
    if not unassigned:
        return JSONResponse({"suggestions": []})

    prompt = _build_topic_suggestions_prompt(topics, unassigned)
    try:
        import asyncio
        response = await asyncio.to_thread(call_llm, config, "", prompt)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    suggestions = _parse_topic_suggestions(response, set(topics), set(unassigned))
    return JSONResponse({"suggestions": suggestions})


@router.get("/settings/test-profile-query")
async def test_profile_query(request: Request, query: str = "", recent: bool = False):
    require_auth(request)
    query = query.strip()
    if not query:
        return JSONResponse({"error": "No query provided."}, status_code=400)
    config = request.app.state.config
    api_key = (config.get("ncbi_api_key") or "").strip()
    import requests as _requests
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": 0,
        "retmode": "json",
    }
    if recent:
        params["reldate"] = 90
        params["datetype"] = "pdat"
    if api_key:
        params["api_key"] = api_key
    try:
        r = _requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        esearch = data.get("esearchresult", {})
        if esearch.get("errorlist"):
            errs = esearch["errorlist"].get("phraseerrors") or esearch["errorlist"].get("fielderrors") or []
            if errs:
                return JSONResponse({"error": "; ".join(str(e) for e in errs)})
        count = int(esearch.get("count", 0))
        return JSONResponse({"count": count})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
            return flash_redirect("/settings?tab=account", "Wrong current password.", "danger")
        if new_password != confirm_password:
            return flash_redirect("/settings?tab=account", "Passwords do not match.", "danger")
        if len(new_password) < 8:
            return flash_redirect("/settings?tab=account", "Password must be at least 8 characters.", "danger")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user["user_id"]),
        )
    current_token = request.cookies.get(SESSION_COOKIE)
    invalidate_other_sessions(user["user_id"], keep_token=current_token)
    return flash_redirect("/settings?tab=account", "Password changed successfully.")
