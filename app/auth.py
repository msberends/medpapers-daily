import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, HTTPException, Request

from app.db import conn_ctx

SESSION_COOKIE = "pd_session"
SESSION_DAYS = 90


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


def require_auth(request: Request):
    user = get_current_user(request)
    if user is None:
        from fastapi.responses import RedirectResponse
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_admin(request: Request):
    user = require_auth(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def clean_expired_sessions():
    now = datetime.now(timezone.utc).isoformat()
    with conn_ctx() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
