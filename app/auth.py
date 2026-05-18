import hashlib
import os
import secrets
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, HTTPException, Request

from app.db import conn_ctx

SESSION_COOKIE = "pd_session"
SESSION_DAYS = 90

# Used when the username is not found, so verify_password always runs (constant-time defence)
_DUMMY_HASH: str = bcrypt.hashpw(b"dummy-constant-time-guard", bcrypt.gensalt()).decode()

# In-memory brute-force rate limiter (per client IP)
_failed_attempts: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
_RATE_WINDOW_SECONDS = 300   # 5-minute sliding window
_RATE_LIMIT_MAX_FAILURES = 10


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_DAYS)
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at, expires_at, last_seen_at) VALUES (?,?,?,?,?)",
            (user_id, token_hash, now.isoformat(), expires.isoformat(), now.isoformat()),
        )
    return token


def delete_session(token: str):
    token_hash = _hash_token(token)
    with conn_ctx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    with conn_ctx() as conn:
        row = conn.execute(
            """SELECT s.user_id, s.expires_at, u.username, u.is_admin, u.display_name
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < now:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now.isoformat(), token_hash),
        )
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "display_name": row["display_name"] or "",
    }


class LoginRequired(Exception):
    pass


def require_auth(request: Request):
    user = get_current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_admin(request: Request):
    user = require_auth(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def invalidate_other_sessions(user_id: int, keep_token: str | None = None) -> None:
    """Delete all sessions for a user, optionally keeping the current one."""
    keep_hash = _hash_token(keep_token) if keep_token else None
    with conn_ctx() as conn:
        if keep_hash:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, keep_hash),
            )
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def check_login_rate_limit(ip: str) -> bool:
    """Return True if the IP may attempt login, False if rate-limited."""
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - _RATE_WINDOW_SECONDS
    with _rate_lock:
        _failed_attempts[ip] = [t for t in _failed_attempts[ip] if t > cutoff]
        return len(_failed_attempts[ip]) < _RATE_LIMIT_MAX_FAILURES


def record_failed_login(ip: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    with _rate_lock:
        _failed_attempts[ip].append(now)


def record_successful_login(ip: str) -> None:
    with _rate_lock:
        _failed_attempts.pop(ip, None)


def clean_expired_sessions():
    now = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
