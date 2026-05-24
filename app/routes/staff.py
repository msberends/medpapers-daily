"""Staff publication tracker — admin-only routes."""
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.auth import get_current_user, require_admin
from app.db import conn_ctx
from app.flash import flash_redirect
from app.pubmed import (
    computed_author_pmids,
    fetch_pubmed_records,
    load_scopus_mapping,
    parse_article,
    search_pubmed,
    upsert_paper,
)

BASE_DIR = Path(__file__).parent.parent.parent
router = APIRouter()

_FETCH_RETMAX = 500


def _split_csv(s: str) -> list[str]:
    return [v.strip() for v in (s or "").split(",") if v.strip()]


def _load_config() -> dict:
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f) or {}


def _get_staff_or_404(conn, staff_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM staff WHERE id = ? AND active = 1", (staff_id,)
    ).fetchone()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Staff member not found")
    return dict(row)


def _load_all_groups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM staff_groups ORDER BY name"
    ).fetchall()]


def _load_member_group_ids(conn, staff_id: int) -> set[int]:
    return {
        r["group_id"]
        for r in conn.execute(
            "SELECT group_id FROM staff_group_members WHERE staff_id = ?", (staff_id,)
        ).fetchall()
    }


def _save_group_memberships(conn, staff_id: int, group_ids: list[int]) -> None:
    conn.execute("DELETE FROM staff_group_members WHERE staff_id = ?", (staff_id,))
    for gid in group_ids:
        conn.execute(
            "INSERT OR IGNORE INTO staff_group_members (group_id, staff_id) VALUES (?, ?)",
            (gid, staff_id),
        )


def _fetch_and_store_papers(
    staff_id: int,
    pmids: list[str],
    api_key: str,
    scopus_mapping: dict,
    status: str = "pending",
    author_last: str = "",
    author_initials: str = "",
    seed_pmids: set[str] | None = None,
) -> int:
    """Fetch records for pmids, upsert into papers, insert staff_papers rows.

    When author_last and author_initials are given, papers where no author
    matches are silently dropped — except seed_pmids, which are always kept.
    Returns the count of newly inserted rows.
    """
    if not pmids:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for i in range(0, len(pmids), 200):
        batch = pmids[i : i + 200]
        try:
            xml_root = fetch_pubmed_records(batch, api_key)
        except Exception:
            continue
        time.sleep(0.12)
        parsed = [
            r for el in xml_root.findall(".//PubmedArticle")
            if (r := parse_article(el)) is not None
        ]
        with conn_ctx() as conn:
            for record in parsed:
                if author_last and author_initials:
                    is_seed = seed_pmids and record["pmid"] in seed_pmids
                    if not is_seed and _find_author_index(
                        record["authors"], author_last, author_initials
                    ) is None:
                        continue
                upsert_paper(conn, record, scopus_mapping)
                score = _compute_author_score(record["authors"], author_last, author_initials) if author_last else None
                result = conn.execute(
                    """INSERT OR IGNORE INTO staff_papers (staff_id, pmid, status, reviewed_at, author_score)
                       VALUES (?, ?, ?, ?, ?)""",
                    (staff_id, record["pmid"], status,
                     now_iso if status == "confirmed" else None, score),
                )
                if result.rowcount:
                    inserted += 1
    return inserted


def _collect_pmids_for_member(member: dict, api_key: str) -> list[str]:
    """Return de-duplicated PMIDs from Computed Authors API + name fallback.

    author_last, author_initials, and seed_pmids may each be comma-separated;
    all permutations are queried.
    """
    all_pmids: list[str] = []
    lasts = _split_csv(member.get("author_last") or "")
    inits = _split_csv(member.get("author_initials") or "")
    if member.get("orcid"):
        # [auid] searches PubMed's actual index — complete and authoritative for ORCID
        try:
            from datetime import date, timedelta
            today = date.today()
            mindate = str(today - timedelta(days=365 * 30)).replace("-", "/")
            maxdate = str(today).replace("-", "/")
            all_pmids.extend(search_pubmed(
                f"{member['orcid']}[auid]", mindate, maxdate, api_key, retmax=_FETCH_RETMAX,
            ))
        except Exception:
            pass
    for seed in _split_csv(member.get("seed_pmids") or ""):
        all_pmids.append(seed)  # seed is a known paper — always include it
        for last in lasts:
            for init in inits:
                all_pmids.extend(computed_author_pmids(seed, last, init, api_key))
    seen: set[str] = set()
    unique: list[str] = []
    for p in all_pmids:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:_FETCH_RETMAX]


def _compute_author_score(authors_json: str, author_last: str, author_initials: str) -> float | None:
    """1st or last author = 2, 2nd or 2nd-last = 1, middle = 0.5, not found = None.

    Falls back to last-name-only when ForeName storage causes initials mismatch
    (e.g. stored 'Matthijs' derives 'm', but configured initials are 'MS').
    """
    idx = _find_author_index(authors_json, author_last, author_initials)
    if idx is None:
        idx = _find_author_index(authors_json, author_last, author_initials, last_only=True)
    if idx is None:
        return None
    try:
        n = len(json.loads(authors_json))
    except Exception:
        return None
    if n == 0:
        return None
    if idx == 0 or idx == n - 1:
        return 2.0
    if idx == 1 or idx == n - 2:
        return 1.0
    return 0.5


def _normalise_initials(forename: str) -> str:
    """Derive comparable initials from a PubMed ForeName or Initials field.

    "MS" → "ms",  "M S" → "ms",  "Matthijs S" → "ms",  "MJ" → "mj"
    """
    words = forename.strip().split()
    if not words:
        return ""
    # Single short all-caps token like "MS", "MJ" — use directly
    token = words[0].replace(".", "")
    if len(words) == 1 and len(token) <= 5 and token.upper() == token:
        return token.lower()
    # Full name like "Matthijs S" or abbreviated "M S" — take first letter of each word
    return "".join(w[0].lower() for w in words if w)


def _author_matches(stored_init: str, configured_inits: list[str]) -> bool:
    """True when stored_init is a prefix of (or equal to) any configured initial.

    This accepts truncated PubMed records ("M" stored, "MS" configured) while
    rejecting genuinely different initials ("MJ" stored, "MS" configured).
    """
    if not stored_init:
        return False
    return any(stored_init == cfg for cfg in configured_inits)


def _find_author_index(authors_json: str, last: str, initials: str,
                       last_only: bool = False) -> int | None:
    try:
        authors = json.loads(authors_json)
    except Exception:
        return None
    lasts = [v.lower() for v in _split_csv(last)]
    inits = [v.lower() for v in _split_csv(initials)]
    for i, author in enumerate(authors):
        parts = author.split(",", 1)
        if not parts:
            continue
        if parts[0].strip().lower() not in lasts:
            continue
        if last_only:
            return i
        stored_init = _normalise_initials(parts[1] if len(parts) > 1 else "")
        if _author_matches(stored_init, inits):
            return i
    return None


# ── Identity search ───────────────────────────────────────────────────────────

@router.get("/staff/search-identity")
async def staff_search_identity(
    request: Request, last: str = "", initials: str = "", offset: int = 0,
) -> JSONResponse:
    require_admin(request)
    if not last or not initials:
        return JSONResponse({"papers": [], "has_more": False})
    cfg = _load_config()
    api_key = cfg.get("ncbi_api_key", "")
    from datetime import date, timedelta
    today = date.today()
    mindate = str(today - timedelta(days=365 * 10)).replace("-", "/")
    maxdate = str(today).replace("-", "/")
    lasts = _split_csv(last)
    inits = _split_csv(initials)
    # Fetch one extra beyond the page end so we can signal has_more without a
    # second network call.
    fetch_retmax = offset + 11
    seen_pmids: set[str] = set()
    all_pmids: list[str] = []
    try:
        for l in lasts:
            for i in inits:
                for p in search_pubmed(f"{l} {i}[au]", mindate, maxdate, api_key,
                                       retmax=fetch_retmax):
                    if p not in seen_pmids:
                        seen_pmids.add(p)
                        all_pmids.append(p)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    batch_pmids = all_pmids[offset:offset + 10]
    has_more = len(all_pmids) > offset + 10
    if not batch_pmids:
        return JSONResponse({"papers": [], "has_more": False})
    try:
        xml_root = fetch_pubmed_records(batch_pmids, api_key)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    lasts_lower = [l.lower() for l in lasts]
    inits_lower = [i.lower() for i in inits]
    papers = []
    for el in xml_root.findall(".//PubmedArticle"):
        record = parse_article(el)
        if record is None:
            continue
        try:
            authors_list = json.loads(record["authors"])
        except Exception:
            authors_list = []
        author_orcids = record.get("author_orcids", [])
        matched_orcid = None
        for idx, author in enumerate(authors_list):
            parts = author.split(",", 1)
            author_last_lower = parts[0].strip().lower()
            rest = parts[1].strip().lower() if len(parts) > 1 else ""
            if author_last_lower in lasts_lower:
                for init_l in inits_lower:
                    if rest.startswith(init_l[:1]):
                        if idx < len(author_orcids):
                            matched_orcid = author_orcids[idx]
                        break
                break
        year = (record.get("epub_date") or record.get("pub_date") or "")[:4]
        papers.append({
            "pmid": record["pmid"],
            "title": record["title"],
            "journal": record["journal"],
            "year": year,
            "authors": authors_list[:8],
            "orcid": matched_orcid,
        })
    return JSONResponse({"papers": papers, "has_more": has_more})


# ── Groups management ─────────────────────────────────────────────────────────

@router.post("/staff/groups")
async def group_add(request: Request):
    require_admin(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return flash_redirect("/staff", "Group name required.", "danger")
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        try:
            conn.execute(
                "INSERT INTO staff_groups (name, created_at) VALUES (?, ?)", (name, now_iso)
            )
        except Exception:
            return flash_redirect("/staff", f"Group '{name}' already exists.", "warning")
    return flash_redirect("/staff", f"Group '{name}' created.")


@router.post("/staff/groups/{group_id:int}/delete")
async def group_delete(group_id: int, request: Request):
    require_admin(request)
    with conn_ctx() as conn:
        conn.execute("DELETE FROM staff_groups WHERE id = ?", (group_id,))
    return flash_redirect("/staff", "Group deleted.")


# ── Staff list ────────────────────────────────────────────────────────────────

@router.get("/staff", response_class=HTMLResponse)
async def staff_list(request: Request):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        members = conn.execute(
            """SELECT s.*,
                  SUM(CASE WHEN sp.status='confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                  SUM(CASE WHEN sp.status='probable'  THEN 1 ELSE 0 END) AS probable_count,
                  SUM(CASE WHEN sp.status='pending'   THEN 1 ELSE 0 END) AS pending_count,
                  ROUND(SUM(CASE WHEN sp.status IN ('confirmed','probable') THEN COALESCE(sp.author_score,0) ELSE 0 END),1) AS total_score,
                  ROUND(SUM(CASE WHEN sp.status IN ('confirmed','probable') AND sp.author_score IS NOT NULL
                                      AND p.scopus_citescore IS NOT NULL AND p.scopus_percentile IS NOT NULL
                                 THEN sp.author_score * p.scopus_percentile / 100.0 ELSE 0 END),2) AS total_quartile_score
               FROM staff s
               LEFT JOIN staff_papers sp ON sp.staff_id = s.id
               LEFT JOIN papers p ON p.pmid = sp.pmid
               WHERE s.active = 1
               GROUP BY s.id
               ORDER BY s.name"""
        ).fetchall()
        all_groups = _load_all_groups(conn)
        # Group names per staff member
        group_rows = conn.execute(
            """SELECT sgm.staff_id, GROUP_CONCAT(sg.name, ', ') AS group_names
               FROM staff_group_members sgm
               JOIN staff_groups sg ON sg.id = sgm.group_id
               GROUP BY sgm.staff_id"""
        ).fetchall()
    group_names_by_staff = {r["staff_id"]: r["group_names"] for r in group_rows}
    member_list = []
    for m in members:
        d = dict(m)
        d["group_names"] = group_names_by_staff.get(m["id"], "")
        member_list.append(d)
    return request.app.state.templates.TemplateResponse(
        request, "staff_list.html",
        {"user": user, "config": request.app.state.config,
         "members": member_list, "all_groups": all_groups},
    )


# ── Add staff member ──────────────────────────────────────────────────────────

@router.get("/staff/add", response_class=HTMLResponse)
async def staff_add_form(request: Request):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        all_groups = _load_all_groups(conn)
    return request.app.state.templates.TemplateResponse(
        request, "staff_add.html",
        {"user": user, "config": request.app.state.config,
         "all_groups": all_groups, "member_group_ids": set()},
    )


@router.post("/staff/add")
async def staff_add(request: Request):
    require_admin(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    author_last = (form.get("author_last") or "").strip()
    author_initials = (form.get("author_initials") or "").strip()
    seed_pmids_raw = (form.get("seed_pmids") or "").strip()
    orcid = (form.get("orcid") or "").strip() or None
    group_ids = [int(g) for g in form.getlist("group_ids") if g.isdigit()]
    identity_mode = (form.get("identity_mode") or "").strip()

    if not name or not author_last or not author_initials:
        return flash_redirect("/staff/add", "Name, last name, and initials are required.", "danger")

    identity_confidence = "probable" if identity_mode == "probable" else None
    paper_status = "probable" if identity_mode == "probable" else "pending"

    now_iso = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        conn.execute(
            """INSERT INTO staff (name, author_last, author_initials, seed_pmids, orcid,
               active, created_at, identity_confidence)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (name, author_last, author_initials, seed_pmids_raw or None, orcid,
             now_iso, identity_confidence),
        )
        staff_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _save_group_memberships(conn, staff_id, group_ids)

    cfg = _load_config()
    api_key = cfg.get("ncbi_api_key", "")
    scopus_mapping = load_scopus_mapping(BASE_DIR)
    member = {"author_last": author_last, "author_initials": author_initials,
              "seed_pmids": seed_pmids_raw, "orcid": orcid}
    unique_pmids = _collect_pmids_for_member(member, api_key)
    queued = _fetch_and_store_papers(
        staff_id, unique_pmids, api_key, scopus_mapping, status=paper_status,
        author_last=author_last, author_initials=author_initials,
        seed_pmids=set(_split_csv(seed_pmids_raw)),
    )

    if identity_mode == "probable":
        msg = f"Staff member added. {queued} paper(s) loaded as probable — no per-paper review needed."
    else:
        msg = f"Staff member added. {queued} paper(s) queued for review."
    if not unique_pmids:
        msg += " No papers found — an ORCID or at least one seed PMID (cauthor_id) is required to fetch papers."
    dest = f"/staff/{staff_id}" if identity_mode == "probable" else f"/staff/{staff_id}/review"
    return flash_redirect(dest, msg)


# ── Staff profile ─────────────────────────────────────────────────────────────

@router.get("/staff/{staff_id:int}", response_class=HTMLResponse)
async def staff_profile(staff_id: int, request: Request, page: int = 1):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        member = _get_staff_or_404(conn, staff_id)
        confirmed_count = conn.execute(
            "SELECT COUNT(*) FROM staff_papers WHERE staff_id=? AND status='confirmed'",
            (staff_id,),
        ).fetchone()[0]
        probable_count = conn.execute(
            "SELECT COUNT(*) FROM staff_papers WHERE staff_id=? AND status='probable'",
            (staff_id,),
        ).fetchone()[0]
        total = confirmed_count + probable_count
        pending = conn.execute(
            "SELECT COUNT(*) FROM staff_papers WHERE staff_id=? AND status='pending'",
            (staff_id,),
        ).fetchone()[0]
        page_size = 50
        offset = (max(1, page) - 1) * page_size
        total_pages = max(1, (total + page_size - 1) // page_size)
        papers = conn.execute(
            """SELECT p.pmid, p.title, p.journal, p.pub_date, p.epub_date,
                      p.scopus_quartile, p.scopus_citescore, p.scopus_percentile,
                      p.doi, p.authors, p.affiliations, sp.status AS paper_status,
                      sp.author_score
               FROM staff_papers sp
               JOIN papers p ON p.pmid = sp.pmid
               WHERE sp.staff_id = ? AND sp.status IN ('confirmed', 'probable')
               ORDER BY COALESCE(p.epub_date, p.pub_date) DESC
               LIMIT ? OFFSET ?""",
            (staff_id, page_size, offset),
        ).fetchall()
        score_row = conn.execute(
            """SELECT SUM(sp.author_score) AS total_score,
                      SUM(CASE WHEN p.scopus_citescore IS NOT NULL AND p.scopus_percentile IS NOT NULL
                               THEN sp.author_score * p.scopus_percentile / 100.0 ELSE 0 END) AS total_quartile_score
               FROM staff_papers sp
               JOIN papers p ON p.pmid = sp.pmid
               WHERE sp.staff_id = ? AND sp.status IN ('confirmed', 'probable')
               AND sp.author_score IS NOT NULL""",
            (staff_id,),
        ).fetchone()
        total_score = score_row["total_score"] or 0.0
        total_quartile_score = score_row["total_quartile_score"] or 0.0
        member_group_ids = _load_member_group_ids(conn, staff_id)
        all_groups = _load_all_groups(conn)
        group_names = [
            r["name"] for r in conn.execute(
                """SELECT sg.name FROM staff_group_members sgm
                   JOIN staff_groups sg ON sg.id = sgm.group_id
                   WHERE sgm.staff_id = ? ORDER BY sg.name""",
                (staff_id,),
            ).fetchall()
        ]
    return request.app.state.templates.TemplateResponse(
        request, "staff_profile.html",
        {
            "user": user, "config": request.app.state.config,
            "member": member, "group_names": group_names,
            "papers": [dict(p) for p in papers],
            "total": total, "confirmed_count": confirmed_count,
            "probable_count": probable_count, "pending": pending,
            "page": max(1, page), "total_pages": total_pages,
            "total_score": total_score,
            "total_quartile_score": total_quartile_score,
        },
    )


# ── Edit / delete ─────────────────────────────────────────────────────────────

@router.get("/staff/{staff_id:int}/edit", response_class=HTMLResponse)
async def staff_edit_form(staff_id: int, request: Request):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        member = _get_staff_or_404(conn, staff_id)
        all_groups = _load_all_groups(conn)
        member_group_ids = _load_member_group_ids(conn, staff_id)
    return request.app.state.templates.TemplateResponse(
        request, "staff_add.html",
        {"user": user, "config": request.app.state.config,
         "member": member, "all_groups": all_groups, "member_group_ids": member_group_ids},
    )


@router.post("/staff/{staff_id:int}/edit")
async def staff_edit(staff_id: int, request: Request):
    require_admin(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    author_last = (form.get("author_last") or "").strip()
    author_initials = (form.get("author_initials") or "").strip()
    seed_pmids_raw = (form.get("seed_pmids") or "").strip()
    orcid = (form.get("orcid") or "").strip() or None
    group_ids = [int(g) for g in form.getlist("group_ids") if g.isdigit()]

    if not name or not author_last or not author_initials:
        return flash_redirect(f"/staff/{staff_id}/edit", "Name, last name, and initials are required.", "danger")

    identity_mode = (form.get("identity_mode") or "").strip()
    with conn_ctx() as conn:
        if identity_mode == "probable":
            conn.execute(
                """UPDATE staff SET name=?, author_last=?, author_initials=?,
                   seed_pmids=?, orcid=?, identity_confidence=? WHERE id=?""",
                (name, author_last, author_initials, seed_pmids_raw or None, orcid,
                 "probable", staff_id),
            )
        else:
            conn.execute(
                """UPDATE staff SET name=?, author_last=?, author_initials=?,
                   seed_pmids=?, orcid=? WHERE id=?""",
                (name, author_last, author_initials, seed_pmids_raw or None, orcid, staff_id),
            )
        _save_group_memberships(conn, staff_id, group_ids)
    return flash_redirect(f"/staff/{staff_id}", "Profile updated.")


@router.post("/staff/{staff_id:int}/delete")
async def staff_delete(staff_id: int, request: Request):
    require_admin(request)
    with conn_ctx() as conn:
        conn.execute("UPDATE staff SET active=0 WHERE id=?", (staff_id,))
    return flash_redirect("/staff", "Staff member removed.")


# ── Refresh (re-fetch from API) ───────────────────────────────────────────────

@router.post("/staff/{staff_id:int}/refresh")
async def staff_refresh(staff_id: int, request: Request):
    require_admin(request)
    with conn_ctx() as conn:
        member = _get_staff_or_404(conn, staff_id)
        already = {
            r["pmid"]
            for r in conn.execute(
                "SELECT pmid FROM staff_papers WHERE staff_id = ?", (staff_id,)
            ).fetchall()
        }
    cfg = _load_config()
    api_key = cfg.get("ncbi_api_key", "")
    scopus_mapping = load_scopus_mapping(BASE_DIR)
    unique_pmids = [p for p in _collect_pmids_for_member(member, api_key) if p not in already]
    paper_status = "probable" if member.get("identity_confidence") == "probable" else "pending"
    queued = _fetch_and_store_papers(
        staff_id, unique_pmids, api_key, scopus_mapping, status=paper_status,
        author_last=member["author_last"], author_initials=member["author_initials"],
        seed_pmids=set(_split_csv(member.get("seed_pmids") or "")),
    )
    # Backfill author_score for any existing papers that are missing it
    with conn_ctx() as conn:
        unscored = conn.execute(
            """SELECT sp.pmid, p.authors FROM staff_papers sp
               JOIN papers p ON p.pmid = sp.pmid
               WHERE sp.staff_id = ? AND sp.author_score IS NULL""",
            (staff_id,),
        ).fetchall()
        for row in unscored:
            score = _compute_author_score(row["authors"], member["author_last"], member["author_initials"])
            if score is not None:
                conn.execute(
                    "UPDATE staff_papers SET author_score=? WHERE staff_id=? AND pmid=?",
                    (score, staff_id, row["pmid"]),
                )
    if paper_status == "probable":
        msg = f"Refresh complete. {queued} new paper(s) added as probable."
        return flash_redirect(f"/staff/{staff_id}", msg)
    return flash_redirect(
        f"/staff/{staff_id}/review",
        f"Refresh complete. {queued} new paper(s) queued for review.",
    )


# ── Review ────────────────────────────────────────────────────────────────────

@router.get("/staff/{staff_id:int}/review", response_class=HTMLResponse)
async def staff_review(staff_id: int, request: Request):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        member = _get_staff_or_404(conn, staff_id)
        pending_rows = conn.execute(
            """SELECT p.pmid, p.title, p.journal, p.pub_date, p.epub_date,
                      p.scopus_quartile, p.doi, p.authors, p.affiliations, p.abstract
               FROM staff_papers sp
               JOIN papers p ON p.pmid = sp.pmid
               WHERE sp.staff_id = ? AND sp.status = 'pending'
               ORDER BY COALESCE(p.epub_date, p.pub_date) DESC""",
            (staff_id,),
        ).fetchall()

    papers = []
    for row in pending_rows:
        d = dict(row)
        d["matched_author_idx"] = _find_author_index(
            d["authors"], member["author_last"], member["author_initials"]
        )
        d["matched_affil"] = None
        if d["matched_author_idx"] is not None and d["affiliations"]:
            try:
                aff_data = json.loads(d["affiliations"])
                aff_list = aff_data.get("aff_list", [])
                author_aff = aff_data.get("author_aff", [])
                idx = d["matched_author_idx"]
                if idx < len(author_aff) and author_aff[idx]:
                    d["matched_affil"] = aff_list[author_aff[idx][0]]
            except Exception:
                pass
        papers.append(d)

    return request.app.state.templates.TemplateResponse(
        request, "staff_review.html",
        {"user": user, "config": request.app.state.config,
         "member": member, "papers": papers},
    )


@router.post("/staff/{staff_id:int}/review/{pmid}")
async def staff_review_paper(staff_id: int, pmid: str, request: Request):
    require_admin(request)
    user = get_current_user(request)
    body = await request.json()
    action = body.get("action", "")
    if action not in ("confirm", "reject"):
        return JSONResponse({"ok": False, "error": "Invalid action"}, status_code=400)
    status = "confirmed" if action == "confirm" else "rejected"
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        conn.execute(
            """UPDATE staff_papers SET status=?, reviewed_at=?, reviewed_by=?
               WHERE staff_id=? AND pmid=?""",
            (status, now_iso, user["user_id"], staff_id, pmid),
        )
    return JSONResponse({"ok": True, "status": status})


# ── Report ────────────────────────────────────────────────────────────────────

def _build_report_query(
    staff_id: int, group_id: int, year_from: int, year_to: int, min_quartile: str
) -> tuple[str, list]:
    _QUARTILE_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    min_q_num = _QUARTILE_ORDER.get(min_quartile, 0)

    query = """
        SELECT s.name AS staff_name,
               (SELECT GROUP_CONCAT(sg.name, ', ')
                FROM staff_group_members sgm
                JOIN staff_groups sg ON sg.id = sgm.group_id
                WHERE sgm.staff_id = s.id
                ORDER BY sg.name) AS groups,
               p.pmid, p.title, p.authors, p.journal, p.pub_date, p.epub_date,
               p.scopus_quartile, p.scopus_citescore, p.scopus_percentile, p.doi,
               sp.status AS paper_status, sp.author_score
        FROM staff_papers sp
        JOIN papers p ON p.pmid = sp.pmid
        JOIN staff s ON s.id = sp.staff_id
        WHERE sp.status IN ('confirmed', 'probable') AND s.active = 1
    """
    params: list = []
    if staff_id:
        query += " AND s.id = ?"
        params.append(staff_id)
    if group_id:
        query += " AND s.id IN (SELECT staff_id FROM staff_group_members WHERE group_id = ?)"
        params.append(group_id)
    if year_from:
        query += " AND CAST(SUBSTR(COALESCE(p.epub_date, p.pub_date), 1, 4) AS INTEGER) >= ?"
        params.append(year_from)
    if year_to:
        query += " AND CAST(SUBSTR(COALESCE(p.epub_date, p.pub_date), 1, 4) AS INTEGER) <= ?"
        params.append(year_to)
    if min_q_num:
        query += " AND p.scopus_quartile IN ({})".format(
            ",".join(f"'Q{n}'" for n in range(1, min_q_num + 1))
        )
    query += " ORDER BY s.name, COALESCE(p.epub_date, p.pub_date) DESC"
    return query, params


@router.get("/staff/report", response_class=HTMLResponse)
async def staff_report(
    request: Request,
    staff_id: int = 0,
    group_id: int = 0,
    year_from: int = 0,
    year_to: int = 0,
    min_quartile: str = "",
):
    require_admin(request)
    user = get_current_user(request)
    with conn_ctx() as conn:
        all_members = conn.execute(
            "SELECT id, name FROM staff WHERE active=1 ORDER BY name"
        ).fetchall()
        all_groups = _load_all_groups(conn)
        query, params = _build_report_query(staff_id, group_id, year_from, year_to, min_quartile)
        papers = conn.execute(query, params).fetchall()
    papers_list = [dict(p) for p in papers]
    staff_totals: dict = {}
    for p in papers_list:
        name = p["staff_name"]
        if name not in staff_totals:
            staff_totals[name] = {"score": 0.0, "quartile_score": 0.0}
        if p["author_score"] is not None:
            staff_totals[name]["score"] += p["author_score"]
            if p["scopus_citescore"] is not None and p["scopus_percentile"] is not None:
                staff_totals[name]["quartile_score"] += p["author_score"] * p["scopus_percentile"] / 100
    return request.app.state.templates.TemplateResponse(
        request, "staff_report.html",
        {
            "user": user, "config": request.app.state.config,
            "all_members": [dict(m) for m in all_members],
            "all_groups": all_groups,
            "papers": papers_list,
            "staff_totals": staff_totals,
            "staff_id": staff_id, "group_id": group_id,
            "year_from": year_from, "year_to": year_to,
            "min_quartile": min_quartile,
        },
    )


@router.get("/staff/report/export")
async def staff_report_export(
    request: Request,
    staff_id: int = 0,
    group_id: int = 0,
    year_from: int = 0,
    year_to: int = 0,
    min_quartile: str = "",
):
    require_admin(request)
    with conn_ctx() as conn:
        query, params = _build_report_query(staff_id, group_id, year_from, year_to, min_quartile)
        rows = conn.execute(query, params).fetchall()

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM for Excel compatibility
    import csv as _csv
    writer = _csv.writer(buf)
    writer.writerow(["Staff Name", "Groups", "Title", "Authors", "Journal",
                     "Quartile", "CiteScore", "Percentile", "Score", "Quartile-score", "Pub Date", "DOI", "PMID", "Status"])
    for row in rows:
        try:
            authors_str = "; ".join(json.loads(row["authors"] or "[]"))
        except Exception:
            authors_str = row["authors"] or ""
        pub_date = row["epub_date"] or row["pub_date"] or ""
        cs = row["scopus_citescore"]
        pct = row["scopus_percentile"]
        writer.writerow([
            row["staff_name"],
            row["groups"] or "",
            row["title"],
            authors_str,
            row["journal"],
            row["scopus_quartile"] or "",
            f"{cs:.1f}" if cs is not None else "",
            f"{pct:.1f}" if pct is not None else "",
            f"{row['author_score']:g}" if row["author_score"] is not None else "",
            f"{row['author_score'] * pct / 100:g}" if (row["author_score"] is not None and cs is not None and pct is not None) else "",
            pub_date[:10] if pub_date else "",
            row["doi"] or "",
            row["pmid"],
            row["paper_status"],
        ])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="publications_{today}.csv"'},
    )
