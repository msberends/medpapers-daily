import os
import re
import zoneinfo
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote_plus, urlparse, urlunparse

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import db
from app.flags import affil_flag_html
from app.flash import _COOKIE as _FLASH_COOKIE, _decode as _flash_decode
from app.auth import (
    SESSION_COOKIE, _DUMMY_HASH, check_login_rate_limit, clean_expired_sessions,
    create_session, delete_session, get_current_user, record_failed_login,
    record_successful_login, verify_password,
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


from functools import lru_cache as _lru_cache


@_lru_cache(maxsize=32)
def _user_cfg_cached(username: str, mtime: float) -> dict:
    path = os.path.join(BASE_DIR, "users", f"{username}.yaml")
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _user_cfg_for_request(user: dict | None) -> dict:
    if not user:
        return {}
    username = user.get("username", "")
    if not username:
        return {}
    path = os.path.join(BASE_DIR, "users", f"{username}.yaml")
    try:
        mtime = os.path.getmtime(path)
        return _user_cfg_cached(username, mtime)
    except Exception:
        return {}


def get_user_theme(user: dict | None, config: dict) -> str:
    """Return the effective Bootswatch theme for the current user, falling back to the admin default."""
    default = config.get("bootstrap_theme", "flatly")
    return _user_cfg_for_request(user).get("bootstrap_theme") or default


def get_user_theme_mode(user: dict | None, config: dict) -> str:
    """Return 'dark', 'light' (default), or 'system' for the current user."""
    mode = _user_cfg_for_request(user).get("theme_mode", "")
    return mode if mode in ("dark", "system") else "light"


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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _startup(app)
    yield


app = FastAPI(title="MedPapers Daily", lifespan=_lifespan)

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response


class _FlashMiddleware(BaseHTTPMiddleware):
    """Read the flash cookie once per request and clear it in the response."""
    async def dispatch(self, request, call_next):
        raw = request.cookies.get(_FLASH_COOKIE, "")
        request.state.flash_messages = _flash_decode(raw)
        response = await call_next(request)
        if raw:
            response.delete_cookie(_FLASH_COOKIE, samesite="lax")
        return response


app.add_middleware(_FlashMiddleware)
app.add_middleware(_SecurityHeaders)


def _startup(app: FastAPI):
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
    app.state.templates.env.globals["get_user_theme_mode"] = get_user_theme_mode
    app.state.templates.env.globals["valid_themes"] = VALID_THEMES
    app.state.templates.env.filters["from_json"] = __import__("json").loads
    app.state.templates.env.filters["norm_pub_date"] = _norm_pub_date
    app.state.templates.env.filters["urlenc"] = quote_plus
    app.state.templates.env.filters["clean_journal"] = lambda s: re.sub(r"\s*\([^)]*\)", "", s or "").strip()

    def inline_md(text: str) -> "markupsafe.Markup":
        from markupsafe import escape, Markup
        s = str(escape(text))
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
        s = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        return Markup(s)

    app.state.templates.env.filters["inline_md"] = inline_md

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

    def publisher_display(publisher: str, publisher_map: dict, predatory: list) -> "markupsafe.Markup":
        from markupsafe import escape, Markup
        if not publisher:
            return Markup("")
        short = escape((publisher_map or {}).get(publisher, publisher))
        if publisher in (predatory or []):
            return Markup(
                f'<span class="text-danger opacity-75" data-bs-toggle="tooltip" '
                f'data-bs-placement="top" title="Marked as \'predatory\'">{short}</span>'
            )
        return Markup(short)

    app.state.templates.env.filters["publisher_display"] = publisher_display

    _ALLOWED_TITLE_TAGS = frozenset({"sub", "sup", "i", "b", "em", "strong", "u"})

    def safe_title(text: str) -> "markupsafe.Markup":
        """Escape HTML but restore a safe allow-list of tags (no attributes)."""
        from markupsafe import escape, Markup
        escaped = str(escape(text or ""))

        def _restore(m: re.Match) -> str:
            slash, tag = m.group(1), m.group(2).lower()
            if tag in _ALLOWED_TITLE_TAGS:
                return f"<{slash}{tag}>"
            return m.group(0)

        return Markup(re.sub(r"&lt;(/?)([a-zA-Z]+)&gt;", _restore, escaped))

    app.state.templates.env.filters["safe_title"] = safe_title

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
    app.state.templates.env.filters["affil_flag_html"] = affil_flag_html

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
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/feed")
    config = app.state.config
    return app.state.templates.TemplateResponse(request, "login.html", {
        "config": config,
    })


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    from app.db import conn_ctx
    client_ip = (request.client.host if request.client else "unknown")
    if not check_login_rate_limit(client_ip):
        from app.flash import flash_redirect as _flash_redirect
        return _flash_redirect("/login", "Too many failed attempts. Try again later.", "danger")
    username = username.strip().lower()
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    # Always run bcrypt to prevent username enumeration via timing
    password_hash = row["password_hash"] if row else _DUMMY_HASH
    valid = verify_password(password, password_hash)
    if row is None or not valid:
        record_failed_login(client_ip)
        from app.flash import flash_redirect as _flash_redirect
        return _flash_redirect("/login", "Invalid username or password.", "danger")
    record_successful_login(client_ip)
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
