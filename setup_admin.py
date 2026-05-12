#!/usr/bin/env python3
"""
One-time setup script: creates the initial admin user in the database.
Run once after installing the application.

Usage:
    python setup_admin.py <username> <password>

Example:
    python setup_admin.py matthijs changeme123
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import yaml
from datetime import datetime, timezone
from app import db
from app.auth import hash_password
from app.db import conn_ctx


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1].strip().lower()
    password = sys.argv[2]

    with open(BASE_DIR / "config.yaml") as f:
        config = yaml.safe_load(f) or {}

    db_path = str(BASE_DIR / config.get("db_path", "data/paperdigest.db"))
    db.init_db(db_path)

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = hash_password(password)

    with conn_ctx() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET password_hash = ?, is_admin = 1 WHERE username = ?",
                (pw_hash, username),
            )
            print(f"Updated existing user '{username}' — set password and admin=1.")
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,1,?)",
                (username, pw_hash, now),
            )
            print(f"Created admin user '{username}'.")


if __name__ == "__main__":
    main()
