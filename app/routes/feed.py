import json
import re
from collections import Counter
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_auth, SESSION_COOKIE
from app.db import conn_ctx
from app.export import export_ris, export_nbib
from app.flags import extract_country, _BORDER_CODES
from app.user_config import load_user_cfg as _get_user_yaml
from app.utils import classify_paper as _classify_paper, get_relevance_cfg as _get_relevance_cfg

router = APIRouter()


_SORT_MAP: dict[str, str] = {
    "newest":    "up.added_at DESC",
    "oldest":    "up.added_at ASC",
    "quartile":  ("CASE p.scopus_quartile WHEN 'Q1' THEN 1 WHEN 'Q2' THEN 2 "
                  "WHEN 'Q3' THEN 3 WHEN 'Q4' THEN 4 ELSE 5 END, up.added_at DESC"),
    "citescore": "p.scopus_citescore DESC NULLS LAST, up.added_at DESC",
}


def _sort_clause(sort: str) -> str:
    return _SORT_MAP.get(sort, _SORT_MAP["newest"])


def _build_feed_where(user_id: int, view: str,
                      quartile: str, date_filter: str,
                      date_from: str, date_to: str, search: str,
                      folder_id: Optional[int],
                      profile_id: int = 0, q2_hard: bool = False,
                      group_by_profile: bool = False,
                      sort: str = "newest") -> tuple[str, list, str]:
    """Return (WHERE clause, params, ORDER BY clause) for feed queries."""
    conditions = ["up.user_id = ?"]
    params: list = [user_id]

    if view == "unread":
        conditions.append("up.is_read = 0")
    elif view == "unrated":
        conditions.append("up.relevance IS NULL")
    elif view == "starred":
        conditions.append("up.is_starred = 1")
    elif view == "folder" and folder_id is not None:
        conditions.append("up.folder_id = ?")
        params.append(folder_id)

    if q2_hard:
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
        # Use FTS5 for fast full-text search; fall back to LIKE on error
        conditions.append(
            "p.pmid IN (SELECT pmid FROM papers_fts WHERE papers_fts MATCH ?)"
        )
        params.append(search)

    where = " AND ".join(conditions)
    order_by = (
        "sp.name NULLS LAST, " + _sort_clause(sort)
        if group_by_profile and not profile_id
        else _sort_clause(sort)
    )
    return where, params, order_by


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
    show_flags_feed = user_yaml.get("show_flags_feed", True)
    author_list_style = user_yaml.get("author_list_style", "truncate")
    show_export_ris = user_yaml.get("show_export_ris", True)
    show_export_nbib = user_yaml.get("show_export_nbib", False)
    feed_group_by_profile = user_yaml.get("feed_group_by_profile", False)
    page_size = user_yaml.get("page_size", 50)
    if page_size not in (10, 25, 50, 100, 150, 200, 500):
        page_size = 50

    selected_topics = topics.split(",") if topics else []

    if sort not in ("newest", "oldest", "quartile", "citescore"):
        sort = "newest"

    where, params, order_by = _build_feed_where(
        user["user_id"], view, quartile,
        date_filter, date_from, date_to, search, folder_id,
        profile_id, q2_hard, feed_group_by_profile, sort,
    )

    rel_cfg = _get_relevance_cfg(user_yaml)

    # Lightweight analytics query: fetch small columns for all matching rows.
    # Full paper data (abstract, affiliations, highlights) is fetched only for the current page.
    analytics_sql = f"""
        SELECT up.pmid, p.mesh_terms, p.keywords, up.added_at AS user_added_at,
               p.scopus_quartile, p.scopus_citescore, sp.name AS search_profile
        FROM user_papers up
        JOIN papers p ON p.pmid = up.pmid
        LEFT JOIN search_profiles sp ON sp.id = up.search_profile_id
        WHERE {where}
        ORDER BY {order_by}
    """

    with conn_ctx() as conn:
        analytics_rows = conn.execute(analytics_sql, params).fetchall()

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
        unrated_count = conn.execute(
            "SELECT COUNT(*) FROM user_papers WHERE user_id = ? AND relevance IS NULL",
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

        # Topic classification and filtering on lightweight rows (no full paper data yet)
        all_topic_set: set = set()
        matching_rows: list = []
        for row in analytics_rows:
            paper_topics = _classify_paper(row["mesh_terms"], mesh_topic_map, row["keywords"])
            all_topic_set.update(paper_topics)
            if not paper_topics:
                paper_topics = ["Unclassified"]
            if selected_topics:
                show = any(t in paper_topics for t in selected_topics) or \
                       ("Unclassified" in selected_topics and paper_topics == ["Unclassified"])
                if not show:
                    continue
            matching_rows.append((dict(row), paper_topics))

        # Analytics from the full filtered set
        today_date = datetime.now(timezone.utc).date()
        topic_counts: dict = Counter(t for _, topics in matching_rows for t in topics)
        quartile_breakdown: dict = Counter(
            (r.get("scopus_quartile") or "Unranked") for r, _ in matching_rows
        )
        daily_raw: dict = Counter(
            r["user_added_at"][:10] for r, _ in matching_rows if r.get("user_added_at")
        )
        daily_counts = [
            {"date": str(today_date - timedelta(days=i)),
             "count": daily_raw.get(str(today_date - timedelta(days=i)), 0)}
            for i in range(6, -1, -1)
        ]

        # Paginate the matched list, then fetch full paper data for just this page
        total = len(matching_rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size
        page_items = matching_rows[offset:offset + page_size]
        page_pmids = [r["pmid"] for r, _ in page_items]

        full_data: dict = {}
        if page_pmids:
            ph = ",".join("?" * len(page_pmids))
            full_sql = f"""
                SELECT p.*, up.is_read, up.is_starred, up.folder_id, up.ris_exported_at,
                       up.added_at AS user_added_at, up.search_profile_id, up.relevance,
                       sp.name AS search_profile
                FROM user_papers up
                JOIN papers p ON p.pmid = up.pmid
                LEFT JOIN search_profiles sp ON sp.id = up.search_profile_id
                WHERE up.user_id = ? AND up.pmid IN ({ph})
            """
            for full_row in conn.execute(full_sql, [user["user_id"]] + page_pmids):
                full_data[full_row["pmid"]] = dict(full_row)

    # Build display page from page_items order, enriching with full paper data
    papers_page = []
    for analytics_row, paper_topics in page_items:
        pmid = analytics_row["pmid"]
        paper_dict = full_data.get(pmid)
        if not paper_dict:
            continue
        _raw_highlights = json.loads(paper_dict.get("highlights") or "null") or []
        paper_dict["highlights"] = [re.sub(r"^[-•*]\s+", "", h) for h in _raw_highlights]
        _abs_struct = paper_dict.get("abstract_structured")
        paper_dict["abstract_sections"] = json.loads(_abs_struct) if _abs_struct else None
        affil_raw = json.loads(paper_dict.get("affiliations") or "null") or {}
        aff_list = affil_raw.get("aff_list", [])
        author_aff_map = affil_raw.get("author_aff", [])
        authors_list = json.loads(paper_dict.get("authors") or "[]")
        author_isos: list = []
        author_iso_names: list = []
        for i in range(len(authors_list)):
            aff_indices = author_aff_map[i] if i < len(author_aff_map) else []
            if aff_indices and aff_list and aff_indices[0] < len(aff_list):
                result = extract_country(aff_list[aff_indices[0]])
                author_isos.append(result[0] if result else None)
                author_iso_names.append(result[1] if result else None)
            else:
                author_isos.append(None)
                author_iso_names.append(None)
        paper_dict["author_isos"] = author_isos
        paper_dict["author_iso_names"] = author_iso_names
        if paper_dict.get("user_added_at"):
            _d = _date.fromisoformat(paper_dict["user_added_at"][:10])
            paper_dict["days_ago"] = (today_date - _d).days
        else:
            paper_dict["days_ago"] = None
        papers_page.append((paper_dict, paper_topics))

    all_topics = sorted(all_topic_set) + ["Unclassified"]
    _colour_cycle = ["blue", "purple", "green", "orange", "teal", "red", "indigo",
                     "yellow", "pink", "cyan", "primary", "success", "danger",
                     "warning", "info", "secondary"]
    _user_colours = user_yaml.get("mesh_topic_colours", {})
    topic_color_map = {
        topic: _user_colours.get(topic, _colour_cycle[i % len(_colour_cycle)])
        for i, topic in enumerate(all_topics)
    }

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
        "unrated_count": unrated_count,
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
        "show_flags_feed": show_flags_feed,
        "author_list_style": author_list_style,
        "show_export_ris": show_export_ris,
        "show_export_nbib": show_export_nbib,
        "border_codes": _BORDER_CODES,
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
        "proxy_enabled": bool(
            request.app.state.config.get("proxy_enabled")
            and request.app.state.config.get("proxy_domain")
        ),
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
        ph = ",".join("?" * len(pmid_list))
        uid = user["user_id"]
        if action == "mark_read":
            conn.execute(
                f"UPDATE user_papers SET is_read = 1 WHERE user_id = ? AND pmid IN ({ph})",
                [uid] + pmid_list,
            )
        elif action == "mark_unread":
            conn.execute(
                f"UPDATE user_papers SET is_read = 0 WHERE user_id = ? AND pmid IN ({ph})",
                [uid] + pmid_list,
            )
        elif action == "star":
            conn.execute(
                f"UPDATE user_papers SET is_starred = 1 WHERE user_id = ? AND pmid IN ({ph})",
                [uid] + pmid_list,
            )
        elif action == "unstar":
            conn.execute(
                f"UPDATE user_papers SET is_starred = 0 WHERE user_id = ? AND pmid IN ({ph})",
                [uid] + pmid_list,
            )
        elif action == "assign_folder" and folder_id is not None:
            conn.execute(
                f"UPDATE user_papers SET folder_id = ? WHERE user_id = ? AND pmid IN ({ph})",
                [folder_id, uid] + pmid_list,
            )
        elif action == "remove_folder":
            conn.execute(
                f"UPDATE user_papers SET folder_id = NULL WHERE user_id = ? AND pmid IN ({ph})",
                [uid] + pmid_list,
            )
        elif action == "export_ris":
            ris_content = export_ris(user["user_id"], pmid_list)
            return Response(
                content=ris_content,
                media_type="application/x-research-info-systems",
                headers={"Content-Disposition": "attachment; filename=papers_daily_export.ris"},
            )
        elif action == "export_nbib":
            nbib_content = export_nbib(user["user_id"], pmid_list)
            return Response(
                content=nbib_content,
                media_type="application/nbib",
                headers={"Content-Disposition": "inline; filename=papers_daily_export.nbib"},
            )

    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)


@router.post("/feed/export-view")
async def export_view(request: Request):
    """Export all papers in the current filtered view as RIS (capped at 1000)."""
    user = require_auth(request)
    form = await request.form()
    view = form.get("view", "all")
    quartile = form.get("quartile", "q1q2")
    date_filter = form.get("date_filter", "all")
    date_from = form.get("date_from", "")
    date_to = form.get("date_to", "")
    search = form.get("search", "").strip()
    profile_id_str = form.get("profile_id", "0")
    folder_id_str = form.get("folder_id", "")
    try:
        profile_id = int(profile_id_str)
    except (ValueError, TypeError):
        profile_id = 0
    try:
        folder_id: Optional[int] = int(folder_id_str) if folder_id_str else None
    except (ValueError, TypeError):
        folder_id = None

    topics_str = form.get("topics", "").strip()
    selected_topics = [t.strip() for t in topics_str.split(",") if t.strip()] if topics_str else []

    user_yaml = _get_user_yaml(user["username"])
    q2_hard = user_yaml.get("q2_hard", True)

    where, params, order_by = _build_feed_where(
        user["user_id"], view, quartile,
        date_filter, date_from, date_to, search, folder_id,
        profile_id, q2_hard,
    )
    sql = f"""SELECT p.pmid, p.mesh_terms, p.keywords FROM user_papers up
              JOIN papers p ON p.pmid = up.pmid
              LEFT JOIN (
                  SELECT upp.pmid, sp.name
                  FROM user_paper_profiles upp
                  JOIN search_profiles sp ON sp.id = upp.profile_id
                  WHERE upp.user_id = ?
                  GROUP BY upp.pmid ORDER BY upp.added_at LIMIT 1
              ) sp ON sp.pmid = p.pmid
              WHERE {where}
              ORDER BY {order_by}
              LIMIT 1000"""
    with conn_ctx() as conn:
        rows = conn.execute(sql, [user["user_id"]] + params).fetchall()

    if selected_topics:
        mesh_topic_map = {k.lower(): v for k, v in user_yaml.get("mesh_topic_map", {}).items()}
        pmid_list = []
        for r in rows:
            paper_topics = _classify_paper(r["mesh_terms"], mesh_topic_map, r["keywords"])
            if not paper_topics:
                paper_topics = ["Unclassified"]
            if any(t in paper_topics for t in selected_topics):
                pmid_list.append(r["pmid"])
    else:
        pmid_list = [r["pmid"] for r in rows]

    if not pmid_list:
        return RedirectResponse(request.headers.get("referer", "/feed"), status_code=303)

    from app.export import export_ris as _export_ris
    ris_content = _export_ris(user["user_id"], pmid_list)
    return Response(
        content=ris_content,
        media_type="application/x-research-info-systems",
        headers={"Content-Disposition": "attachment; filename=medpapers_export.ris"},
    )


@router.post("/feed/mark-all-read")
async def mark_all_read(request: Request):
    """Mark all papers in the current filtered view as read."""
    user = require_auth(request)
    form = await request.form()
    view = form.get("view", "unread")
    quartile = form.get("quartile", "all")
    date_filter = form.get("date_filter", "all")
    date_from = form.get("date_from", "")
    date_to = form.get("date_to", "")
    search = form.get("search", "").strip()
    profile_id_str = form.get("profile_id", "0")
    try:
        profile_id = int(profile_id_str)
    except (ValueError, TypeError):
        profile_id = 0
    user_yaml = _get_user_yaml(user["username"])
    q2_hard = user_yaml.get("q2_hard", True)

    where, params, _ = _build_feed_where(
        user["user_id"], view, quartile,
        date_filter, date_from, date_to, search, None,
        profile_id, q2_hard,
    )
    sql = f"""UPDATE user_papers SET is_read = 1
              WHERE (user_id, pmid) IN (
                  SELECT up.user_id, up.pmid
                  FROM user_papers up
                  JOIN papers p ON p.pmid = up.pmid
                  WHERE {where} AND up.is_read = 0
              )"""
    with conn_ctx() as conn:
        conn.execute(sql, params)

    from urllib.parse import urlencode
    qp: dict = {"view": view, "quartile": quartile, "date_filter": date_filter}
    if date_from:
        qp["date_from"] = date_from
    if date_to:
        qp["date_to"] = date_to
    if search:
        qp["search"] = search
    if profile_id:
        qp["profile_id"] = profile_id
    return RedirectResponse("/feed?" + urlencode(qp), status_code=303)


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
    new_val = 0
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
    if request.headers.get("accept", "").find("application/json") != -1:
        return JSONResponse({"is_read": new_val})
    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)


@router.post("/feed/rate-paper/{pmid}")
async def rate_paper(pmid: str, request: Request, relevance: int = Form(...)):
    user = require_auth(request)
    if relevance not in (-1, 0, 1):
        relevance = 0
    new_val = None
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
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"relevance": new_val})
    referer = request.headers.get("referer", "/feed")
    return RedirectResponse(referer, status_code=303)
