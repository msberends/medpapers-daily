import json
from datetime import datetime, timedelta, timezone
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


def _classify_paper(mesh_terms_json: str, mesh_topic_map: dict) -> list[str]:
    if not mesh_terms_json:
        return []
    terms = json.loads(mesh_terms_json)
    topics = set()
    for term in terms:
        if term in mesh_topic_map:
            topics.add(mesh_topic_map[term])
    return sorted(topics)


def _build_feed_query(user_id: int, view: str, topics: list[str],
                      quartile: str, date_filter: str,
                      date_from: str, date_to: str, search: str,
                      folder_id: Optional[int], mesh_topic_map: dict,
                      profile_id: int = 0, q2_hard: bool = False,
                      group_by_profile: bool = False) -> tuple[str, list]:
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
        conditions.append("up.search_profile_id = ?")
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
        "sp.name NULLS LAST, up.added_at DESC"
        if group_by_profile and not profile_id
        else "up.added_at DESC"
    )
    sql = f"""
        SELECT p.*, up.is_read, up.is_starred, up.folder_id, up.ris_exported_at,
               up.added_at as user_added_at, up.search_profile_id,
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
    page: int = 1,
):
    user = require_auth(request)
    user_yaml = _get_user_yaml(user["username"])
    mesh_topic_map = user_yaml.get("mesh_topic_map", {})
    q2_hard = user_yaml.get("q2_hard", True)
    show_quartile = user_yaml.get("show_quartile", True)
    feed_group_by_profile = user_yaml.get("feed_group_by_profile", False)
    page_size = user_yaml.get("page_size", 50)
    if page_size not in (10, 25, 50, 100, 150, 200, 500):
        page_size = 50

    selected_topics = topics.split(",") if topics else []

    sql, params = _build_feed_query(
        user["user_id"], view, selected_topics, quartile,
        date_filter, date_from, date_to, search, folder_id, mesh_topic_map,
        profile_id, q2_hard, feed_group_by_profile,
    )

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

    papers_with_topics = []
    all_topic_set = set()
    for row in rows:
        paper_topics = _classify_paper(row["mesh_terms"], mesh_topic_map)
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
    topic_color_map = {topic: i % 8 for i, topic in enumerate(all_topics)}

    total = len(papers_with_topics)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    papers_page = papers_with_topics[offset:offset + page_size]

    # Base URL for pagination links — all current filters, no page param
    qp: dict = {"view": view, "quartile": quartile, "date_filter": date_filter}
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
