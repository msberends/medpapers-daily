import json
from collections import Counter
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_auth, SESSION_COOKIE
from app.db import conn_ctx
from app.export import export_ris

router = APIRouter()


def _get_user_yaml(username: str) -> dict:
    import yaml, os
    path = os.path.join("users", f"{username}.yaml")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _get_relevance_cfg(user_yaml: dict) -> dict:
    return {
        "enabled": user_yaml.get("relevance_alert_enabled", True),
        "threshold": user_yaml.get("relevance_alert_threshold", 0.30),
        "min_rated": user_yaml.get("relevance_alert_min_rated", 10),
        "lookback_days": user_yaml.get("relevance_alert_lookback_days", 30),
    }


def _sort_clause(sort: str) -> str:
    if sort == "oldest":
        return "up.added_at ASC"
    if sort == "quartile":
        return ("CASE p.scopus_quartile WHEN 'Q1' THEN 1 WHEN 'Q2' THEN 2 "
                "WHEN 'Q3' THEN 3 WHEN 'Q4' THEN 4 ELSE 5 END, up.added_at DESC")
    if sort == "citescore":
        return "p.scopus_citescore DESC NULLS LAST, up.added_at DESC"
    return "up.added_at DESC"


def _classify_paper(mesh_terms_json: str, mesh_topic_map: dict,
                    keywords_json: str = "[]") -> list[str]:
    terms = json.loads(mesh_terms_json or "[]") + json.loads(keywords_json or "[]")
    topics = {mesh_topic_map[t.lower()] for t in terms if t.lower() in mesh_topic_map}
    return sorted(topics)


def _build_feed_query(user_id: int, view: str, topics: list[str],
                      quartile: str, date_filter: str,
                      date_from: str, date_to: str, search: str,
                      folder_id: Optional[int], mesh_topic_map: dict,
                      profile_id: int = 0, q2_hard: bool = False,
                      group_by_profile: bool = False,
                      sort: str = "newest") -> tuple[str, list]:
    conditions = ["up.user_id = ?"]
    params: list = [user_id]

    if view == "unread":
        conditions.append("up.is_read = 0")
    elif view == "starred":
        conditions.append("up.is_starred = 1")
    elif view == "folder" and folder_id is not None:
        conditions.append("up.folder_id = ?")
        params.append(folder_id)

    if q2_hard:
        # Hard baseline: never show Q3/Q4/unranked regardless of the URL filter
        if quartile == "q1":
            conditions.append("p.scopus_quartile = 'Q1'")
        else:
            conditions.append("p.scopus_quartile IN ('Q1', 'Q2')")
    else:
        if quartile == "q1":
            conditions.append("p.scopus_quartile = 'Q1'")
        elif quartile == "q1q2":
            conditions.append("(p.scopus_quartile = 'Q1' OR p.scopus_quartile = 'Q2')")

    if profile_id:
        conditions.append(
            "EXISTS (SELECT 1 FROM user_paper_profiles upp"
            " WHERE upp.user_id = up.user_id AND upp.pmid = up.pmid AND upp.profile_id = ?)"
        )
        params.append(profile_id)

    today = datetime.now(timezone.utc).date()
    if date_filter == "today":
        conditions.append("date(up.added_at) = ?")
        params.append(str(today))
    elif date_filter == "7days":
        cutoff = str(today - timedelta(days=7))
        conditions.append("date(up.added_at) >= ?")
        params.append(cutoff)
    elif date_filter == "30days":
        cutoff = str(today - timedelta(days=30))
        conditions.append("date(up.added_at) >= ?")
        params.append(cutoff)
    elif date_filter == "custom":
        if date_from:
            conditions.append("date(up.added_at) >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("date(up.added_at) <= ?")
            params.append(date_to)

    if search:
        conditions.append("(p.title LIKE ? OR p.abstract LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where = " AND ".join(conditions)
    order_by = (
        "sp.name NULLS LAST, " + _sort_clause(sort)
        if group_by_profile and not profile_id
        else _sort_clause(sort)
    )
    sql = f"""
        SELECT p.*, up.is_read, up.is_starred, up.folder_id, up.ris_exported_at,
               up.added_at as user_added_at, up.search_profile_id, up.relevance,
               sp.name as search_profile
        FROM user_papers up
        JOIN papers p ON p.pmid = up.pmid
        LEFT JOIN search_profiles sp ON sp.id = up.search_profile_id
        WHERE {where}
        ORDER BY {order_by}
    """
    return sql, params


@router.get("/feed", response_class=HTMLResponse)
async def feed(
    request: Request,
    view: str = "unread",
    folder_id: Optional[int] = None,
    topics: Optional[str] = None,
    quartile: str = "q1q2",
    date_filter: str = "all",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    profile_id: int = 0,
    sort: str = "newest",
    page: int = 1,
):
    user = require_auth(request)
    user_yaml = _get_user_yaml(user["username"])
    mesh_topic_map = {k.lower(): v for k, v in user_yaml.get("mesh_topic_map", {}).items()}
    q2_hard = user_yaml.get("q2_hard", True)
    show_quartile = user_yaml.get("show_quartile", True)
    feed_group_by_profile = user_yaml.get("feed_group_by_profile", False)
    page_size = user_yaml.get("page_size", 50)
    if page_size not in (10, 25, 50, 100, 150, 200, 500):
        page_size = 50

    selected_topics = topics.split(",") if topics else []

    if sort not in ("newest", "oldest", "quartile", "citescore"):
        sort = "newest"

    sql, params = _build_feed_query(
        user["user_id"], view, selected_topics, quartile,
        date_filter, date_from, date_to, search, folder_id, mesh_topic_map,
        profile_id, q2_hard, feed_group_by_profile, sort,
    )

    rel_cfg = _get_relevance_cfg(user_yaml)

    with conn_ctx() as conn:
        rows = conn.execute(sql, params).fetchall()
        all_profiles = [
            dict(r) for r in conn.execute(
                "SELECT id, name FROM search_profiles WHERE user_id = ? ORDER BY name",
                (user["user_id"],),
            ).fetchall()
        ]
        folders = conn.execute(
            "SELECT * FROM folders WHERE user_id = ? ORDER BY name", (user["user_id"],)
        ).fetchall()
        folder_counts = {
            r["folder_id"]: r["cnt"]
            for r in conn.execute(
                "SELECT folder_id, COUNT(*) as cnt FROM user_papers WHERE user_id = ? AND folder_id IS NOT NULL GROUP BY folder_id",
                (user["user_id"],),
            ).fetchall()
        }
        if q2_hard:
            unread_count = conn.execute(
                """SELECT COUNT(*) FROM user_papers up
                   JOIN papers p ON p.pmid = up.pmid
                   WHERE up.user_id = ? AND up.is_read = 0
                   AND p.scopus_quartile IN ('Q1', 'Q2')""",
                (user["user_id"],),
            ).fetchone()[0]
        else:
            unread_count = conn.execute(
                "SELECT COUNT(*) FROM user_papers WHERE user_id = ? AND is_read = 0",
                (user["user_id"],),
            ).fetchone()[0]
        starred_count = conn.execute(
            "SELECT COUNT(*) FROM user_papers WHERE user_id = ? AND is_starred = 1",
            (user["user_id"],),
        ).fetchone()[0]
        total_user_papers = conn.execute(
            "SELECT COUNT(*) FROM user_papers WHERE user_id = ?",
            (user["user_id"],),
        ).fetchone()[0]

        profile_alerts = []
        if rel_cfg["enabled"]:
            threshold = float(rel_cfg["threshold"])
            min_rated = int(rel_cfg["min_rated"])
            date_modifier = f"-{int(rel_cfg['lookback_days'])} days"
            alert_rows = conn.execute(
                """SELECT sp.id, sp.name,
                          SUM(CASE WHEN up.relevance = 1  THEN 1 ELSE 0 END) AS relevant,
                          SUM(CASE WHEN up.relevance = -1 THEN 1 ELSE 0 END) AS not_relevant,
                          SUM(CASE WHEN up.relevance IS NOT NULL THEN 1 ELSE 0 END) AS rated
                   FROM search_profiles sp
                   JOIN user_paper_profiles upp
                        ON upp.profile_id = sp.id AND upp.user_id = sp.user_id
                   JOIN user_papers up
                        ON up.user_id = upp.user_id AND up.pmid = upp.pmid
                   WHERE sp.user_id = ? AND date(upp.added_at) >= date('now', ?)
                   GROUP BY sp.id, sp.name""",
                (user["user_id"], date_modifier),
            ).fetchall()
            for ar in alert_rows:
                if ar["rated"] >= min_rated and ar["relevant"] / ar["rated"] < threshold:
                    profile_alerts.append({
                        "id": ar["id"],
                        "name": ar["name"],
                        "relevant": ar["relevant"],
                        "not_relevant": ar["not_relevant"],
                        "rated": ar["rated"],
                        "pct": round(ar["relevant"] / ar["rated"] * 100),
                    })

    papers_with_topics = []
    all_topic_set = set()
    for row in rows:
        paper_topics = _classify_paper(row["mesh_terms"], mesh_topic_map, row["keywords"])
        all_topic_set.update(paper_topics)
        if not paper_topics:
            paper_topics = ["Unclassified"]
        if selected_topics:
            show = any(t in paper_topics for t in selected_topics) or \
                   ("Unclassified" in selected_topics and paper_topics == ["Unclassified"])
            if not show:
                continue
        papers_with_topics.append((dict(row), paper_topics))

    all_topics = sorted(all_topic_set) + ["Unclassified"]
    _colour_cycle = ["blue","purple","green","orange","teal","red","indigo","yellow","pink","cyan","primary","success","danger","warning","info","secondary"]
    _user_colours = user_yaml.get("mesh_topic_colours", {})
    topic_color_map = {
        topic: _user_colours.get(topic, _colour_cycle[i % len(_colour_cycle)])
        for i, topic in enumerate(all_topics)
    }

    # Derived analytics computed from the full filtered set (before pagination)
    today_date = datetime.now(timezone.utc).date()

    topic_counts: dict = Counter()
    for _paper, _topics in papers_with_topics:
        for _t in _topics:
            topic_counts[_t] += 1

    quartile_breakdown: dict = Counter(
        (_paper.get("scopus_quartile") or "Unranked")
        for _paper, _ in papers_with_topics
    )

    daily_raw: dict = Counter(
        _paper["user_added_at"][:10]
        for _paper, _ in papers_with_topics
        if _paper.get("user_added_at")
    )
    daily_counts = [
        {"date": str(today_date - timedelta(days=i)),
         "count": daily_raw.get(str(today_date - timedelta(days=i)), 0)}
        for i in range(6, -1, -1)
    ]

    for _paper, _ in papers_with_topics:
        if _paper.get("user_added_at"):
            _d = _date.fromisoformat(_paper["user_added_at"][:10])
            _paper["days_ago"] = (today_date - _d).days
        else:
            _paper["days_ago"] = None

    total = len(papers_with_topics)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    papers_page = papers_with_topics[offset:offset + page_size]

    # Base URL for pagination links — all current filters, no page param
    qp: dict = {"view": view, "quartile": quartile, "date_filter": date_filter, "sort": sort}
    if search:
        qp["search"] = search
    if selected_topics:
        qp["topics"] = ",".join(selected_topics)
    if folder_id is not None:
        qp["folder_id"] = folder_id
    if profile_id:
        qp["profile_id"] = profile_id
    if date_filter == "custom":
        if date_from:
            qp["date_from"] = date_from
        if date_to:
            qp["date_to"] = date_to
    page_url_base = "/feed?" + urlencode(qp) + "&page="

    return request.app.state.templates.TemplateResponse(request, "feed.html", {
        "user": user,
        "papers": papers_page,
        "folders": [dict(f) for f in folders],
        "folder_counts": folder_counts,
        "unread_count": unread_count,
        "starred_count": starred_count,
        "all_topics": all_topics,
        "topic_color_map": topic_color_map,
        "selected_topics": selected_topics,
        "all_profiles": all_profiles,
        "profile_id": profile_id,
        "group_by_profile": feed_group_by_profile,
        "view": view,
        "folder_id": folder_id,
        "quartile": quartile,
        "date_filter": date_filter,
        "date_from": date_from,
        "date_to": date_to,
        "search": search,
        "show_quartile": show_quartile,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "page_url_base": page_url_base,
        "total_user_papers": total_user_papers,
        "sort": sort,
        "topic_counts": dict(topic_counts),
        "quartile_breakdown": dict(quartile_breakdown),
        "daily_counts": daily_counts,
        "profile_alerts": profile_alerts,
        "rel_cfg": rel_cfg,
        "config": request.app.state.config,
    })


@router.post("/feed/bulk-action")
async def bulk_action(
    request: Request,
    action: str = Form(...),
    pmids: str = Form(...),
    folder_id: Optional[int] = Form(None),
):
    user = require_auth(request)
    pmid_list = [p.strip() for p in pmids.split(",") if p.strip()]
    if not pmid_list:
        return RedirectResponse("/feed", status_code=303)

    with conn_ctx() as conn:
        if action == "mark_read":
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET is_read = 1 WHERE user_id = ? AND pmid = ?",
                    (user["user_id"], pmid),
                )
        elif action == "mark_unread":
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET is_read = 0 WHERE user_id = ? AND pmid = ?",
                    (user["user_id"], pmid),
                )
        elif action == "star":
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET is_starred = 1 WHERE user_id = ? AND pmid = ?",
                    (user["user_id"], pmid),
                )
        elif action == "unstar":
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET is_starred = 0 WHERE user_id = ? AND pmid = ?",
                    (user["user_id"], pmid),
                )
        elif action == "assign_folder" and folder_id is not None:
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET folder_id = ? WHERE user_id = ? AND pmid = ?",
                    (folder_id, user["user_id"], pmid),
                )
        elif action == "remove_folder":
            for pmid in pmid_list:
                conn.execute(
                    "UPDATE user_papers SET folder_id = NULL WHERE user_id = ? AND pmid = ?",
                    (user["user_id"], pmid),
                )
        elif action == "export_ris":
            ris_content = export_ris(user["user_id"], pmid_list)
            return Response(
                content=ris_content,
                media_type="application/x-research-info-systems",
                headers={"Content-Disposition": "attachment; filename=papers_daily_export.ris"},
            )

    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)


@router.post("/feed/toggle-star/{pmid}")
async def toggle_star(pmid: str, request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT is_starred FROM user_papers WHERE user_id = ? AND pmid = ?",
            (user["user_id"], pmid),
        ).fetchone()
        if row:
            new_val = 0 if row["is_starred"] else 1
            conn.execute(
                "UPDATE user_papers SET is_starred = ? WHERE user_id = ? AND pmid = ?",
                (new_val, user["user_id"], pmid),
            )
    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)


@router.post("/feed/toggle-read/{pmid}")
async def toggle_read(pmid: str, request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT is_read FROM user_papers WHERE user_id = ? AND pmid = ?",
            (user["user_id"], pmid),
        ).fetchone()
        if row:
            new_val = 0 if row["is_read"] else 1
            conn.execute(
                "UPDATE user_papers SET is_read = ? WHERE user_id = ? AND pmid = ?",
                (new_val, user["user_id"], pmid),
            )
    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)


@router.post("/feed/rate-paper/{pmid}")
async def rate_paper(pmid: str, request: Request, relevance: int = Form(...)):
    user = require_auth(request)
    if relevance not in (-1, 0, 1):
        relevance = 0
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT relevance FROM user_papers WHERE user_id = ? AND pmid = ?",
            (user["user_id"], pmid),
        ).fetchone()
        if row:
            # Toggle: clicking the already-active rating clears it
            if relevance != 0 and row["relevance"] == relevance:
                new_val = None
            else:
                new_val = relevance if relevance != 0 else None
            conn.execute(
                "UPDATE user_papers SET relevance = ? WHERE user_id = ? AND pmid = ?",
                (new_val, user["user_id"], pmid),
            )
    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)
