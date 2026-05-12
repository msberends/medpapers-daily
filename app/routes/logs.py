from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth import require_auth
from app.db import conn_ctx

router = APIRouter()
PAGE_SIZE = 50


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, page: int = 1, filter_user_id: int = 0):
    user = require_auth(request)
    offset = (page - 1) * PAGE_SIZE

    with conn_ctx() as conn:
        if user["is_admin"]:
            uid_filter = filter_user_id or None

            if uid_filter:
                total = conn.execute(
                    "SELECT COUNT(*) FROM fetch_log WHERE user_id = ?",
                    (uid_filter,),
                ).fetchone()[0]
                fetch_rows = conn.execute(
                    """SELECT fl.*, u.username FROM fetch_log fl
                       JOIN users u ON u.id = fl.user_id
                       WHERE fl.user_id = ?
                       ORDER BY fl.run_at DESC LIMIT ? OFFSET ?""",
                    (uid_filter, PAGE_SIZE, offset),
                ).fetchall()
                mail_rows = conn.execute(
                    """SELECT ml.*, u.username FROM mail_log ml
                       LEFT JOIN users u ON u.id = ml.user_id
                       WHERE ml.user_id = ?
                       ORDER BY ml.sent_at DESC LIMIT 100""",
                    (uid_filter,),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
                fetch_rows = conn.execute(
                    """SELECT fl.*, u.username FROM fetch_log fl
                       JOIN users u ON u.id = fl.user_id
                       ORDER BY fl.run_at DESC LIMIT ? OFFSET ?""",
                    (PAGE_SIZE, offset),
                ).fetchall()
                mail_rows = conn.execute(
                    """SELECT ml.*, u.username FROM mail_log ml
                       LEFT JOIN users u ON u.id = ml.user_id
                       ORDER BY ml.sent_at DESC LIMIT 100""",
                ).fetchall()

            all_users = conn.execute(
                "SELECT id, username FROM users ORDER BY username"
            ).fetchall()
        else:
            uid = user["user_id"]
            total = conn.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE user_id = ?", (uid,)
            ).fetchone()[0]
            fetch_rows = conn.execute(
                """SELECT fl.*, u.username FROM fetch_log fl
                   JOIN users u ON u.id = fl.user_id
                   WHERE fl.user_id = ?
                   ORDER BY fl.run_at DESC LIMIT ? OFFSET ?""",
                (uid, PAGE_SIZE, offset),
            ).fetchall()
            mail_rows = conn.execute(
                """SELECT ml.*, u.username FROM mail_log ml
                   LEFT JOIN users u ON u.id = ml.user_id
                   WHERE ml.user_id = ?
                   ORDER BY ml.sent_at DESC LIMIT 100""",
                (uid,),
            ).fetchall()
            all_users = []

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return request.app.state.templates.TemplateResponse(request, "logs.html", {
        "user": user,
        "logs": [dict(r) for r in fetch_rows],
        "mail_logs": [dict(r) for r in mail_rows],
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "filter_user_id": filter_user_id,
        "all_users": [dict(u) for u in all_users],
        "config": request.app.state.config,
    })
