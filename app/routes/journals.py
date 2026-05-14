import csv
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import require_auth

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


@router.get("/journals", response_class=HTMLResponse)
async def journals_page(request: Request):
    require_auth(request)
    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    has_data = csv_path.exists()
    last_updated = None
    if has_data:
        mtime = csv_path.stat().st_mtime
        last_updated = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return request.app.state.templates.TemplateResponse(request, "journals.html", {
        "user":         request.app.state.get_current_user(request),
        "config":       request.app.state.config,
        "has_data":     has_data,
        "last_updated": last_updated,
    })


@router.get("/journals/data")
async def journals_data(
    request: Request,
    q: str = "",
    page: int = 1,
    per_page: int = 100,
):
    require_auth(request)
    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    if not csv_path.exists():
        return JSONResponse({"rows": [], "total": 0, "page": 1, "pages": 0, "per_page": per_page})

    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                q_num = int(float(row.get("quartile") or ""))
                quartile = f"Q{q_num}" if 1 <= q_num <= 4 else None
            except (ValueError, TypeError):
                quartile = None
            try:
                citescore: float | None = float(row.get("citescore") or "")
            except (ValueError, TypeError):
                citescore = None
            try:
                percentile: float | None = float(row.get("percentile") or "")
            except (ValueError, TypeError):
                percentile = None
            try:
                coverage_start: int | None = int(row.get("coverageStartYear") or "")
            except (ValueError, TypeError):
                coverage_start = None
            rows.append({
                "title":          row.get("title")             or "",
                "issn":           row.get("issn")              or "",
                "eissn":          row.get("eIssn")             or "",
                "quartile":       quartile,
                "citescore":      citescore,
                "percentile":     percentile,
                "publisher":      row.get("publisher")         or "",
                "source_id":      row.get("source-id")         or "",
                "coverage_start": coverage_start,
            })

    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in r["title"].lower()
                or ql in r["publisher"].lower()
                or ql in r["issn"].lower()
                or ql in r["eissn"].lower()]

    rows.sort(key=lambda r: (r["citescore"] is None, -(r["citescore"] or 0)))

    total    = len(rows)
    per_page = max(10, min(per_page, 500))
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(1, min(page, pages))
    start    = (page - 1) * per_page
    return JSONResponse({
        "rows":     rows[start:start + per_page],
        "total":    total,
        "page":     page,
        "pages":    pages,
        "per_page": per_page,
    })
