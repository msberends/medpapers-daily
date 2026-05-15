#!/usr/bin/env python3
"""
Standalone cron script: generate LLM highlights for papers that do not have them yet.
Reads LLM configuration from config.yaml. Uses a PID lock to prevent parallel runs.
Invoked directly or launched in the background by fetch.py / the admin UI.
"""
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from app.llm import call_llm, parse_highlights, DEFAULT_HIGHLIGHTS_PROMPT

STATUS_PATH = BASE_DIR / "data" / "llm_status.json"
PID_PATH = BASE_DIR / "data" / "llm.pid"


def _write_status(data: dict):
    STATUS_PATH.parent.mkdir(exist_ok=True)
    STATUS_PATH.write_text(json.dumps(data))


def _acquire_lock() -> bool:
    """Return True if the lock was acquired; False if another instance is running."""
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)  # signal 0 = existence check only
            return False
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale PID file
    PID_PATH.write_text(str(os.getpid()))
    return True


def _release_lock():
    PID_PATH.unlink(missing_ok=True)


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def _conn_ctx(db_path: str):
    conn = _get_conn(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    config_path = BASE_DIR / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    provider = config.get("llm_provider", "")
    if not provider:
        print("[llm] No LLM provider configured. Exiting.", file=sys.stderr, flush=True)
        return

    if not _acquire_lock():
        print("[llm] Another instance is already running. Exiting.", file=sys.stderr, flush=True)
        return

    db_path = str(BASE_DIR / config.get("db_path", "data/paperdigest.db"))
    system_prompt = (config.get("llm_prompt") or "").strip() or DEFAULT_HIGHLIGHTS_PROMPT

    _write_status({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": 0,
        "processed": 0,
        "errors": 0,
    })

    processed = 0
    errors = 0
    last_error = None

    try:
        with _conn_ctx(db_path) as conn:
            papers = conn.execute(
                "SELECT pmid, abstract FROM papers "
                "WHERE highlights IS NULL AND abstract IS NOT NULL AND abstract != '' "
                "ORDER BY first_seen_at DESC"
            ).fetchall()

        total = len(papers)
        print(f"[llm] {total} paper(s) need highlights.", flush=True)
        _write_status({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "processed": 0,
            "errors": 0,
        })

        for row in papers:
            pmid = row["pmid"]
            abstract = row["abstract"]
            try:
                response = call_llm(config, system_prompt, f"Abstract:\n{abstract}")
                highlights = parse_highlights(response)
                with _conn_ctx(db_path) as conn:
                    conn.execute(
                        "UPDATE papers SET highlights = ? WHERE pmid = ?",
                        (json.dumps(highlights), pmid),
                    )
                processed += 1
                print(f"[llm] {pmid}: {len(highlights)} highlight(s).", flush=True)
                time.sleep(0.25)
            except Exception as e:
                errors += 1
                last_error = str(e)
                print(f"[llm] {pmid}: error — {e}", file=sys.stderr, flush=True)

            _write_status({
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "total": total,
                "processed": processed,
                "errors": errors,
            })

    except Exception as e:
        last_error = str(e)
        print(f"[llm] Fatal error: {e}", file=sys.stderr, flush=True)
        _write_status({
            "status": "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "processed": processed,
            "errors": errors + 1,
            "last_error": str(e),
        })
        _release_lock()
        return

    _write_status({
        "status": "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "processed": processed,
        "errors": errors,
        "last_error": last_error,
    })
    print(f"[llm] Done. processed={processed}, errors={errors}", flush=True)
    _release_lock()


if __name__ == "__main__":
    main()
