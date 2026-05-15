import os
import re
import zoneinfo
from datetime import datetime
from urllib.parse import quote_plus, urlparse, urlunparse

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.auth import (
    SESSION_COOKIE, clean_expired_sessions, create_session,
    delete_session, get_current_user, verify_password,
)
from app.routes import admin, feed, folders, journals, logs, paper, settings
from app.themes import get_theme_url, VALID_THEMES

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MONTH_ABBR = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _norm_pub_date(s: str) -> str:
    """Convert PubMed pub_date strings like '2026-Mar-31' to ISO '2026-03-31'."""
    if not s:
        return s
    parts = s.split("-")
    return "-".join(
        _MONTH_ABBR.get(p[:3].lower(), p) if not p.isdigit() else p
        for p in parts
    )


def get_user_theme(user: dict | None, config: dict) -> str:
    """Return the effective Bootswatch theme for the current user, falling back to the admin default."""
    default = config.get("bootstrap_theme", "flatly")
    if not user:
        return default
    username = user.get("username", "")
    if not username:
        return default
    path = os.path.join(BASE_DIR, "users", f"{username}.yaml")
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("bootstrap_theme") or default
    except Exception:
        return default


def proxy_url(doi: str, config: dict) -> str | None:
    """Return an EZProxy URL for a DOI, or None if proxy is disabled/unconfigured."""
    if not config.get("proxy_enabled") or not doi:
        return None
    proxy_domain = (config.get("proxy_domain") or "").strip()
    if not proxy_domain:
        return None
    url = f"https://doi.org/{doi}"
    url_regex = (config.get("proxy_url_regex") or "").strip()
    if url_regex:
        try:
            if not re.search(url_regex, url):
                return None
        except re.error:
            return None
    parsed = urlparse(url)
    dashed_host = parsed.netloc.replace(".", "-")
    return urlunparse(("https", f"{dashed_host}.{proxy_domain}", parsed.path, "", "", ""))


def load_config() -> tuple[dict, str]:
    config_path = os.path.join(BASE_DIR, "config.yaml")
    with open(config_path) as f:
        raw = f.read()
    return yaml.safe_load(raw) or {}, raw


app = FastAPI(title="MedPapers Daily")


@app.on_event("startup")
async def startup():
    config, config_yaml_str = load_config()
    app.state.config = config
    app.state.config_yaml_str = config_yaml_str
    app.title = config.get("app_name", "MedPapers Daily")

    db_path = os.path.join(BASE_DIR, config.get("db_path", "data/paperdigest.db"))
    db.init_db(db_path)

    templates_dir = os.path.join(BASE_DIR, "app", "templates")
    app.state.templates = Jinja2Templates(directory=templates_dir)
    app.state.templates.env.globals["get_theme_url"] = get_theme_url
    app.state.templates.env.globals["proxy_url"] = proxy_url
    app.state.templates.env.globals["get_user_theme"] = get_user_theme
    app.state.templates.env.globals["valid_themes"] = VALID_THEMES
    app.state.templates.env.filters["from_json"] = __import__("json").loads
    app.state.templates.env.filters["norm_pub_date"] = _norm_pub_date
    app.state.templates.env.filters["urlenc"] = quote_plus
    app.state.templates.env.filters["clean_journal"] = lambda s: re.sub(r"\s*\([^)]*\)", "", s or "").strip()

    def dim_initials(author: str) -> str:
        author = (author or "").strip()
        if "," in author:
            last, _, first = author.partition(",")
            first = first.strip()
            if first:
                parts = first.split()
                split_at = next((i for i, p in enumerate(parts) if len(p) == 1 and p.isupper()), len(parts))
                if split_at < len(parts):
                    first = (" ".join(parts[:split_at]) + " " if split_at else "") + "".join(parts[split_at:])
                return f'<span class="text-body">{last.strip()}</span><span class="text-body-tertiary">, {first}</span>'
            return f'<span class="text-body">{last.strip()}</span>'
        # fallback for "Last AB" initials format
        m = re.match(r'^(.*?)(\s+[A-Z]+)$', author)
        if m:
            return (f'<span class="text-body">{m.group(1)}</span>'
                    f'<span class="text-body-tertiary"> {m.group(2).strip()}</span>')
        return author

    app.state.templates.env.filters["dim_initials"] = dim_initials

    def last_name(author: str) -> str:
        author = (author or "").strip()
        if "," in author:
            return author.partition(",")[0].strip()
        m = re.match(r'^(.*?)(\s+[A-Z]+)$', author)
        if m:
            return m.group(1).strip()
        return author

    app.state.templates.env.filters["last_name"] = last_name

    def publisher_display(publisher: str, publisher_map: dict, predatory: list) -> str:
        if not publisher:
            return ""
        short = (publisher_map or {}).get(publisher, publisher)
        if publisher in (predatory or []):
            return f'<span class="text-danger opacity-75">{short}</span>'
        return short

    app.state.templates.env.filters["publisher_display"] = publisher_display

    def publisher_short_filter(publisher: str, mapping: dict | None = None) -> str:
        if not publisher:
            return ""
        return (mapping or {}).get(publisher, publisher)

    app.state.templates.env.filters["publisher_short"] = publisher_short_filter

    def to_local(dt_str: str) -> str:
        if not dt_str:
            return ""
        tz_name = app.state.config.get("timezone", "UTC")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
            return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return dt_str[:19].replace("T", " ")

    app.state.templates.env.filters["to_local"] = to_local

    def _ordinal_suffix(n: int) -> str:
        n = int(n)
        if 11 <= (n % 100) <= 13:
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

    def ordinal(n) -> str:
        n = int(n)
        return f"{n}{_ordinal_suffix(n)}"

    app.state.templates.env.filters["ordinal"] = ordinal
    app.state.templates.env.filters["ordinal_suffix"] = _ordinal_suffix
    app.state.get_current_user = get_current_user

    clean_expired_sessions()


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)

app.include_router(feed.router)
app.include_router(paper.router)
app.include_router(folders.router)
app.include_router(settings.router)
app.include_router(admin.router)
app.include_router(logs.router)
app.include_router(journals.router)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/feed")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/feed")
    config = app.state.config
    return app.state.templates.TemplateResponse(request, "login.html", {
        "error": error,
        "config": config,
    })


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    from app.db import conn_ctx
    username = username.strip().lower()
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
    token = create_session(row["id"])
    response = RedirectResponse("/feed", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=60 * 60 * 24 * 90,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
