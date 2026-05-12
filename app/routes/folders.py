from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth
from app.db import conn_ctx

router = APIRouter()


@router.get("/folders", response_class=HTMLResponse)
async def folders_page(request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        folders = conn.execute(
            "SELECT * FROM folders WHERE user_id = ? ORDER BY name",
            (user["user_id"],),
        ).fetchall()
        counts = {
            r["folder_id"]: r["cnt"]
            for r in conn.execute(
                "SELECT folder_id, COUNT(*) as cnt FROM user_papers WHERE user_id = ? AND folder_id IS NOT NULL GROUP BY folder_id",
                (user["user_id"],),
            ).fetchall()
        }
    return request.app.state.templates.TemplateResponse(request, "folders.html", {
        "user": user,
        "folders": [dict(f) for f in folders],
        "counts": counts,
        "config": request.app.state.config,
    })


@router.post("/folders/create")
async def create_folder(request: Request, name: str = Form(...)):
    user = require_auth(request)
    name = name.strip()
    if name:
        now = datetime.now(timezone.utc).isoformat()
        with conn_ctx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO folders (user_id, name, created_at) VALUES (?,?,?)",
                (user["user_id"], name, now),
            )
    return RedirectResponse("/folders", status_code=303)


@router.post("/folders/rename/{folder_id}")
async def rename_folder(folder_id: int, request: Request, name: str = Form(...)):
    user = require_auth(request)
    name = name.strip()
    if name:
        with conn_ctx() as conn:
            conn.execute(
                "UPDATE folders SET name = ? WHERE id = ? AND user_id = ?",
                (name, folder_id, user["user_id"]),
            )
    return RedirectResponse("/folders", status_code=303)


@router.post("/folders/delete/{folder_id}")
async def delete_folder(folder_id: int, request: Request):
    user = require_auth(request)
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE user_papers SET folder_id = NULL WHERE folder_id = ? AND user_id = ?",
            (folder_id, user["user_id"]),
        )
        conn.execute(
            "DELETE FROM folders WHERE id = ? AND user_id = ?",
            (folder_id, user["user_id"]),
        )
    return RedirectResponse("/folders", status_code=303)
