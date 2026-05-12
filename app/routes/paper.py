import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_auth
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


@router.get("/paper/{pmid}", response_class=HTMLResponse)
async def paper_detail(pmid: str, request: Request):
    user = require_auth(request)
    user_yaml = _get_user_yaml(user["username"])
    mesh_topic_map = user_yaml.get("mesh_topic_map", {})

    with conn_ctx() as conn:
        paper = conn.execute("SELECT * FROM papers WHERE pmid = ?", (pmid,)).fetchone()
        if paper is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Paper not found")
        up = conn.execute(
            "SELECT * FROM user_papers WHERE user_id = ? AND pmid = ?",
            (user["user_id"], pmid),
        ).fetchone()
        if up:
            conn.execute(
                "UPDATE user_papers SET is_read = 1 WHERE user_id = ? AND pmid = ?",
                (user["user_id"], pmid),
            )
        folders = conn.execute(
            "SELECT * FROM folders WHERE user_id = ? ORDER BY name",
            (user["user_id"],),
        ).fetchall()

    paper = dict(paper)
    paper["authors_list"] = json.loads(paper["authors"] or "[]")
    paper["mesh_list"] = json.loads(paper["mesh_terms"] or "[]")
    topics = _classify_paper(paper["mesh_terms"], mesh_topic_map)
    if not topics:
        topics = ["Unclassified"]
    all_topics = sorted(set(mesh_topic_map.values())) + ["Unclassified"]
    topic_color_map = {t: i % 8 for i, t in enumerate(all_topics)}

    up_dict = dict(up) if up else None
    if up_dict:
        up_dict["is_read"] = 1

    return request.app.state.templates.TemplateResponse(request, "paper.html", {
        "user": user,
        "paper": paper,
        "up": up_dict,
        "folders": [dict(f) for f in folders],
        "topics": topics,
        "topic_color_map": topic_color_map,
        "mesh_topic_map": mesh_topic_map,
        "show_quartile": user_yaml.get("show_quartile", True),
        "journal_metric": user_yaml.get("journal_metric", "if"),
        "config": request.app.state.config,
    })


@router.post("/paper/{pmid}/mark-read")
async def mark_read(pmid: str, request: Request, is_read: int = Form(1)):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET is_read = ? WHERE user_id = ? AND pmid = ?",
            (is_read, user["user_id"], pmid),
        )
    return RedirectResponse(f"/paper/{pmid}", status_code=303)


@router.post("/paper/{pmid}/star")
async def star_paper(pmid: str, request: Request, is_starred: int = Form(1)):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET is_starred = ? WHERE user_id = ? AND pmid = ?",
            (is_starred, user["user_id"], pmid),
        )
    return RedirectResponse(f"/paper/{pmid}", status_code=303)


@router.post("/paper/{pmid}/assign-folder")
async def assign_folder(pmid: str, request: Request, folder_id: Optional[int] = Form(None)):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET folder_id = ? WHERE user_id = ? AND pmid = ?",
            (folder_id, user["user_id"], pmid),
        )
    return RedirectResponse(f"/paper/{pmid}", status_code=303)


@router.get("/export/ris/{pmid}")
async def export_single_ris(pmid: str, request: Request):
    user = require_auth(request)
    ris_content = export_ris(user["user_id"], [pmid])
    return Response(
        content=ris_content,
        media_type="application/x-research-info-systems",
        headers={"Content-Disposition": f"attachment; filename=paper_{pmid}.ris"},
    )
