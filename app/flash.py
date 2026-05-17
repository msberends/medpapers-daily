"""Lightweight cookie-based flash messages.

Flash messages are stored in a signed JSON cookie (`pd_flash`) and consumed
on the next request. The signing uses HMAC-SHA256 with the app secret key
(falls back to a fixed dev key so the app starts without configuration).

Usage:
    # In a route handler (before returning a response):
    from app.flash import flash_redirect
    return flash_redirect("/settings", "Changes saved.", "success")

    # In a template, the `flash_messages` variable is injected by the
    middleware (see app/main.py) and contains a list of
    {"message": ..., "category": ...} dicts.
"""
import hashlib
import hmac
import json
import time
import os
from fastapi.responses import RedirectResponse

_COOKIE = "pd_flash"
_SECRET = os.environ.get("PD_SECRET_KEY", "papersdaily-dev-key-change-in-prod")


def _sign(payload: str) -> str:
    return hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]


def _encode(messages: list[dict]) -> str:
    payload = json.dumps(messages, separators=(",", ":"))
    sig = _sign(payload)
    return f"{sig}.{payload}"


def _decode(value: str) -> list[dict]:
    if not value or "." not in value:
        return []
    sig, _, payload = value.partition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return []
    try:
        data = json.loads(payload)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def flash_redirect(url: str, message: str, category: str = "success",
                   status_code: int = 303) -> RedirectResponse:
    """Redirect to `url` with a flash message stored in a cookie."""
    response = RedirectResponse(url, status_code=status_code)
    response.set_cookie(
        _COOKIE,
        _encode([{"message": message, "category": category}]),
        max_age=60,
        httponly=True,
        samesite="lax",
    )
    return response


def get_flash(request) -> list[dict]:
    """Read and clear the flash cookie from the request."""
    raw = request.cookies.get(_COOKIE, "")
    return _decode(raw)


def consume_flash(response: RedirectResponse, messages: list[dict]) -> None:
    """Append flash messages to an already-created redirect response."""
    existing_raw = ""
    existing = _decode(existing_raw)
    all_msgs = existing + messages
    response.set_cookie(
        _COOKIE,
        _encode(all_msgs),
        max_age=60,
        httponly=True,
        samesite="lax",
    )
