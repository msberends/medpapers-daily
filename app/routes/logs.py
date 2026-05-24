import json
import subprocess
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_auth, require_admin
from app.db import conn_ctx

router = APIRouter()
PAGE_SIZE = 50
MAIL_PAGE_SIZE = 50
LOG_PAGE_LINES = 100
BASE_DIR = Path(__file__).parent.parent.parent


def _page_log(path: Path, page: int = 1, per_page: int = LOG_PAGE_LINES) -> tuple[str, int, int]:
    """Return (text, total_lines, total_pages). Page 1 is the most recent lines."""
    if not path.exists():
        return "", 0, 0
    try:
        lines = path.read_text(errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return "", 0, 0
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        end = total - (page - 1) * per_page
        start = max(0, end - per_page)
        return "\n".join(lines[start:end]), total, total_pages
    except Exception:
        return "", 0, 0


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, page: int = 1, filter_user_id: int = 0,
                    mail_page: int = 1, llm_page: int = 1, scopus_page: int = 1,
                    llm_started: str = "", error: str = ""):
    user = require_auth(request)
    offset = (page - 1) * PAGE_SIZE
    mail_offset = (mail_page - 1) * MAIL_PAGE_SIZE

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
                mail_total = conn.execute(
                    "SELECT COUNT(*) FROM mail_log WHERE user_id = ?", (uid_filter,)
                ).fetchone()[0]
                mail_rows = conn.execute(
                    """SELECT ml.*, u.username FROM mail_log ml
                       LEFT JOIN users u ON u.id = ml.user_id
                       WHERE ml.user_id = ?
                       ORDER BY ml.sent_at DESC LIMIT ? OFFSET ?""",
                    (uid_filter, MAIL_PAGE_SIZE, mail_offset),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
                fetch_rows = conn.execute(
                    """SELECT fl.*, u.username FROM fetch_log fl
                       JOIN users u ON u.id = fl.user_id
                       ORDER BY fl.run_at DESC LIMIT ? OFFSET ?""",
                    (PAGE_SIZE, offset),
                ).fetchall()
                mail_total = conn.execute("SELECT COUNT(*) FROM mail_log").fetchone()[0]
                mail_rows = conn.execute(
                    """SELECT ml.*, u.username FROM mail_log ml
                       LEFT JOIN users u ON u.id = ml.user_id
                       ORDER BY ml.sent_at DESC LIMIT ? OFFSET ?""",
                    (MAIL_PAGE_SIZE, mail_offset),
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
            mail_total = conn.execute(
                "SELECT COUNT(*) FROM mail_log WHERE user_id = ?", (uid,)
            ).fetchone()[0]
            mail_rows = conn.execute(
                """SELECT ml.*, u.username FROM mail_log ml
                   LEFT JOIN users u ON u.id = ml.user_id
                   WHERE ml.user_id = ?
                   ORDER BY ml.sent_at DESC LIMIT ? OFFSET ?""",
                (uid, MAIL_PAGE_SIZE, mail_offset),
            ).fetchall()
            all_users = []

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    scopus_log, scopus_total_lines, scopus_total_pages = ("", 0, 0)
    llm_log, llm_total_lines, llm_total_pages = ("", 0, 0)
    if user["is_admin"]:
        scopus_log, scopus_total_lines, scopus_total_pages = _page_log(
            BASE_DIR / "logs" / "scopus.log", scopus_page
        )
        llm_log, llm_total_lines, llm_total_pages = _page_log(
            BASE_DIR / "logs" / "llm.log", llm_page
        )

    llm_status: dict = {"status": "idle"}
    llm_pending = 0
    if user["is_admin"]:
        status_path = BASE_DIR / "data" / "llm_status.json"
        if status_path.exists():
            try:
                llm_status = json.loads(status_path.read_text())
            except Exception:
                pass
        with conn_ctx() as conn:
            llm_pending = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE highlights IS NULL AND abstract IS NOT NULL AND abstract != ''"
            ).fetchone()[0]

    mail_total_pages = max(1, (mail_total + MAIL_PAGE_SIZE - 1) // MAIL_PAGE_SIZE)

    return request.app.state.templates.TemplateResponse(request, "logs.html", {
        "user": user,
        "logs": [dict(r) for r in fetch_rows],
        "mail_logs": [dict(r) for r in mail_rows],
        "scopus_log": scopus_log,
        "llm_log": llm_log,
        "llm_status": llm_status,
        "llm_pending": llm_pending,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "mail_page": mail_page,
        "mail_total": mail_total,
        "mail_total_pages": mail_total_pages,
        "llm_page": llm_page,
        "llm_total_lines": llm_total_lines,
        "llm_total_pages": llm_total_pages,
        "scopus_page": scopus_page,
        "scopus_total_lines": scopus_total_lines,
        "scopus_total_pages": scopus_total_pages,
        "filter_user_id": filter_user_id,
        "all_users": [dict(u) for u in all_users],
        "config": request.app.state.config,
        "llm_started": llm_started,
        "error": error,
    })


@router.post("/logs/run-llm-highlights")
async def logs_run_llm_highlights(request: Request):
    require_admin(request)
    config = request.app.state.config
    if not config.get("llm_provider"):
        return RedirectResponse(
            "/logs?error=No+LLM+provider+configured#tab-llm", status_code=303
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
        return RedirectResponse("/logs?llm_started=1#tab-llm", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/logs?error={quote(str(e))}#tab-llm", status_code=303)
