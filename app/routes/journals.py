import csv
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import get_current_user
from app.db import conn_ctx

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent

_SORT_KEYS = {"title", "quartile", "citescore", "percentile", "coverage_start", "publisher", "feed_count"}

# Anonymous callers: max 60 requests per 60-second window per IP
_ANON_RATE_WINDOW = 60
_ANON_RATE_MAX    = 60
_ANON_PER_PAGE_MAX = 200

_anon_requests: dict[str, list[float]] = defaultdict(list)
_anon_lock = threading.Lock()


def _check_anon_rate(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _ANON_RATE_WINDOW
    with _anon_lock:
        _anon_requests[ip] = [t for t in _anon_requests[ip] if t > cutoff]
        if len(_anon_requests[ip]) >= _ANON_RATE_MAX:
            return False
        _anon_requests[ip].append(now)
        return True


# Abbreviation map: rebuilt from DB at most once every 5 minutes.
# The map is the same for every user, so there is no reason to query it per request.
_abbrev_cache: tuple[float, dict[str, str]] | None = None
_abbrev_cache_lock = threading.Lock()
_ABBREV_CACHE_TTL = 300


def _get_abbrev_map() -> dict[str, str]:
    global _abbrev_cache
    now = time.monotonic()
    with _abbrev_cache_lock:
        if _abbrev_cache is not None and now - _abbrev_cache[0] < _ABBREV_CACHE_TTL:
            return _abbrev_cache[1]
    with conn_ctx() as conn:
        result: dict[str, str] = {}
        for row in conn.execute(
            "SELECT issn, iso_abbreviation FROM papers "
            "WHERE issn != '' AND iso_abbreviation IS NOT NULL"
        ).fetchall():
            if row["issn"] and row["iso_abbreviation"] and row["issn"] not in result:
                result[row["issn"]] = row["iso_abbreviation"]
    with _abbrev_cache_lock:
        _abbrev_cache = (now, result)
    return result


@lru_cache(maxsize=1)
def _load_scopus_rows(mtime: float) -> list[dict]:
    """Parse the Scopus CSV once per file version; mtime busts the cache on upload."""
    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                q_num = int(float(row.get("quartile") or ""))
                q_label = f"Q{q_num}" if 1 <= q_num <= 4 else None
            except (ValueError, TypeError):
                q_label = None
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

            issn  = row.get("issn")  or ""
            eissn = row.get("eIssn") or ""
            oa = "full" if (row.get("openaccess") or "") == "1" else "pw"

            rows.append({
                "title":          row.get("title")    or "",
                "issn":           issn,
                "eissn":          eissn,
                "quartile":       q_label,
                "citescore":      citescore,
                "percentile":     percentile,
                "publisher":      row.get("publisher") or "",
                "source_id":      row.get("source-id") or "",
                "coverage_start": coverage_start,
                "oa":             oa,
            })
    return rows


@router.get("/journals", response_class=HTMLResponse)
async def journals_page(request: Request):
    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    has_data = csv_path.exists()
    last_updated = None
    if has_data:
        mtime = csv_path.stat().st_mtime
        last_updated = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return request.app.state.templates.TemplateResponse(request, "journals.html", {
        "user":         get_current_user(request),
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
    quartile: str = "",
    sort: str = "citescore",
    dir: str = "desc",
):
    user = get_current_user(request)

    if user is None:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_anon_rate(client_ip):
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(_ANON_RATE_WINDOW)},
            )
        per_page = min(per_page, _ANON_PER_PAGE_MAX)

    csv_path = BASE_DIR / "data" / "scopus_journals.csv"
    if not csv_path.exists():
        return JSONResponse({"rows": [], "total": 0, "page": 1, "pages": 0, "per_page": per_page})

    # Abbreviation map is user-independent; serve from cache
    abbrev_map = _get_abbrev_map()

    # Feed counts are user-specific; only query when authenticated
    feed_map: dict[str, int] = {}
    if user:
        with conn_ctx() as conn:
            for row in conn.execute(
                """SELECT p.issn, COUNT(*) AS cnt
                   FROM papers p JOIN user_papers up ON p.pmid = up.pmid
                   WHERE up.user_id = ? AND p.issn != ''
                   GROUP BY p.issn""",
                (user["user_id"],),
            ).fetchall():
                if row["issn"]:
                    feed_map[row["issn"]] = row["cnt"]

    # Load Scopus rows from cache (parsed once per CSV version)
    mtime = csv_path.stat().st_mtime
    base_rows = _load_scopus_rows(mtime)

    # Merge per-request DB data (abbreviation, feed count) into a shallow copy
    rows = []
    for r in base_rows:
        issn, eissn = r["issn"], r["eissn"]
        rows.append({
            **r,
            "abbreviation": abbrev_map.get(issn) or abbrev_map.get(eissn) or "",
            "feed_count":   feed_map.get(issn, 0) or feed_map.get(eissn, 0),
        })

    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in r["title"].lower()
                or ql in r["publisher"].lower()
                or ql in r["issn"].lower()
                or ql in r["eissn"].lower()
                or ql in r["abbreviation"].lower()]

    if quartile in ("Q1", "Q2", "Q3", "Q4"):
        rows = [r for r in rows if r["quartile"] == quartile]

    # Sorting — nulls always last regardless of direction
    sort_key = sort if sort in _SORT_KEYS else "citescore"
    reverse = (dir != "asc")
    _Q_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, None: 5}
    if sort_key == "quartile":
        rows.sort(key=lambda r: _Q_ORDER.get(r["quartile"], 5), reverse=reverse)
    elif sort_key in ("citescore", "percentile", "coverage_start", "feed_count"):
        if reverse:
            rows.sort(key=lambda r: (r[sort_key] is None, -(r[sort_key] or 0)))
        else:
            rows.sort(key=lambda r: (r[sort_key] is None, r[sort_key] or 0))
    else:
        rows.sort(key=lambda r: (r[sort_key] or "").lower(), reverse=reverse)

    total    = len(rows)
    per_page = max(10, min(per_page, 500 if user else _ANON_PER_PAGE_MAX))
    pages    = max(1, (total + per_page - 1) // per_page)
    page     = max(1, min(page, pages))
    start    = (page - 1) * per_page
    return JSONResponse({
        "rows":     rows[start:start + per_page],
        "total":    total,
        "page":     page,
        "pages":    pages,
        "per_page": per_page,
        "sort":     sort_key,
        "dir":      dir,
        "quartile": quartile,
    })
