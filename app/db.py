import sqlite3
import os
from contextlib import contextmanager

_db_path: str = ""


def init_db(db_path: str):
    global _db_path
    _db_path = db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_conn() as conn:
        _create_tables(conn)
        _migrate(conn)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def conn_ctx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL,
            display_name  TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash  TEXT    NOT NULL UNIQUE,
            created_at  TEXT    NOT NULL,
            expires_at  TEXT    NOT NULL,
            last_seen_at TEXT   NOT NULL
        );

        CREATE TABLE IF NOT EXISTS papers (
            pmid            TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            authors         TEXT NOT NULL,
            journal         TEXT NOT NULL,
            issn            TEXT,
            pub_date        TEXT,
            abstract        TEXT,
            doi             TEXT,
            oa_url          TEXT,
            mesh_terms      TEXT,
            scopus_quartile      TEXT,
            scopus_cites_per_doc REAL,
            scopus_cites_3yr     REAL,
            scopus_sjr           REAL,
            scopus_h_index       INTEGER,
            first_seen_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS search_profiles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name       TEXT    NOT NULL,
            query      TEXT    NOT NULL DEFAULT '',
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS user_papers (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            pmid              TEXT    NOT NULL REFERENCES papers(pmid),
            is_read           INTEGER NOT NULL DEFAULT 0,
            is_starred        INTEGER NOT NULL DEFAULT 0,
            folder_id         INTEGER REFERENCES folders(id) ON DELETE SET NULL,
            ris_exported_at   TEXT,
            added_at          TEXT    NOT NULL,
            search_profile_id INTEGER REFERENCES search_profiles(id) ON DELETE SET NULL,
            emailed_at        TEXT,
            UNIQUE(user_id, pmid)
        );

        CREATE TABLE IF NOT EXISTS fetch_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            run_at        TEXT    NOT NULL,
            papers_found  INTEGER NOT NULL DEFAULT 0,
            papers_new    INTEGER NOT NULL DEFAULT 0,
            status        TEXT    NOT NULL,
            error_message TEXT,
            details       TEXT
        );

        CREATE TABLE IF NOT EXISTS mail_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            sent_at   TEXT    NOT NULL,
            to_addr   TEXT    NOT NULL,
            subject   TEXT    NOT NULL,
            status    TEXT    NOT NULL,
            error     TEXT
        );
    """)
    conn.commit()


def _migrate(conn: sqlite3.Connection):
    """Add columns introduced after the initial schema."""
    users_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "display_name" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")

    papers_cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "epub_date" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN epub_date TEXT")
    if "scopus_cites_per_doc" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN scopus_cites_per_doc REAL")
    if "scopus_cites_3yr" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN scopus_cites_3yr REAL")
    if "scopus_sjr" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN scopus_sjr REAL")
    if "publisher" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN publisher TEXT")
    if "scopus_h_index" not in papers_cols:
        conn.execute("ALTER TABLE papers ADD COLUMN scopus_h_index INTEGER")

    fetchlog_cols = {r[1] for r in conn.execute("PRAGMA table_info(fetch_log)").fetchall()}
    if "details" not in fetchlog_cols:
        conn.execute("ALTER TABLE fetch_log ADD COLUMN details TEXT")

    up_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_papers)").fetchall()}
    if "search_profile" not in up_cols:
        conn.execute("ALTER TABLE user_papers ADD COLUMN search_profile TEXT")

    if "search_profile_id" not in up_cols:
        # Populate search_profiles from existing text data before adding the FK column
        existing = conn.execute(
            """SELECT DISTINCT user_id, search_profile FROM user_papers
               WHERE search_profile IS NOT NULL AND search_profile != ''"""
        ).fetchall()
        for row in existing:
            conn.execute(
                """INSERT OR IGNORE INTO search_profiles
                   (user_id, name, query, enabled, created_at)
                   VALUES (?, ?, '', 1, datetime('now'))""",
                (row["user_id"], row["search_profile"]),
            )
        conn.execute(
            "ALTER TABLE user_papers ADD COLUMN search_profile_id INTEGER REFERENCES search_profiles(id) ON DELETE SET NULL"
        )
        conn.execute(
            """UPDATE user_papers SET search_profile_id = (
                   SELECT sp.id FROM search_profiles sp
                   WHERE sp.user_id = user_papers.user_id
                   AND sp.name = user_papers.search_profile
               )
               WHERE search_profile IS NOT NULL AND search_profile != ''"""
        )

    if "emailed_at" not in up_cols:
        conn.execute(
            "ALTER TABLE user_papers ADD COLUMN emailed_at TEXT"
        )
        # Mark all existing rows as already emailed to avoid a one-time flood
        # on first digest run after this migration.
        conn.execute(
            "UPDATE user_papers SET emailed_at = added_at WHERE added_at IS NOT NULL"
        )

    conn.commit()
