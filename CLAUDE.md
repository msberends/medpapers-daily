# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the web app (development)
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 2711 --reload

# Install dependencies
venv/bin/pip install -r requirements.txt

# Create initial admin user
venv/bin/python setup_admin.py <username> <password>

# Run cron scripts manually (from project root)
venv/bin/python fetch.py
venv/bin/python email_digest.py

# Deploy as systemd service
sudo cp papersdaily.service /etc/systemd/system/
sudo systemctl enable --now papersdaily
sudo journalctl -u papersdaily -f
```

There is no test suite.

## Architecture

**Papers Daily** is a self-hosted scientific literature monitor. It fetches papers from PubMed, filters by Scopus journal quartile, and delivers daily email digests. Multi-user, with per-user PubMed search profiles.

### Stack
- FastAPI + Jinja2 templates (Bootstrap 5 / Bootswatch)
- SQLite (no ORM — raw SQL throughout via `app/db.py`)
- bcrypt for passwords (used directly, not via passlib — incompatible with bcrypt 5.0 on Python 3.12)
- Cron-only scheduling (no background threads or task queues)
- Starlette 1.0.0 `TemplateResponse` signature: `TemplateResponse(request, name, context)`

### Data flow
1. `fetch.py` (cron): reads `users/*.yaml`, queries PubMed via NCBI E-utilities, filters Q3/Q4 papers using `data/scopus.csv` (SCImago CSV), calls Unpaywall for OA URLs, writes to SQLite.
2. `email_digest.py` (cron, runs after fetch): queries SQLite for each user's unread papers, sends HTML digest via SMTP.
3. FastAPI app: serves the web UI for browsing, starring, foldering, and exporting papers.

### Configuration
- `config.yaml` — global settings (NCBI API key, SMTP, port, timezone, Bootswatch theme)
- `users/<username>.yaml` — per-user settings: email, PubMed search profiles, MeSH→topic map, quartile filter flags (`q2_hard`, `q1_soft`), `fetch_enabled`

### Key files
- `app/main.py` — FastAPI app, startup (DB init, template setup, custom Jinja2 filters `to_local` and `from_json`)
- `app/db.py` — `init_db()`, `conn_ctx()` context manager (commit/rollback/close), `_migrate()` for additive schema changes
- `app/auth.py` — session cookie `pd_session`, SHA-256 token hashing, 90-day sessions stored in `sessions` table
- `app/routes/` — one file per route group: `feed`, `paper`, `folders`, `settings`, `admin`, `logs`
- `fetch.py` / `email_digest.py` — **standalone** cron scripts; do not import from `app/` except `fetch.py` calls `app.db.init_db()` at startup

### DB schema (SQLite, `data/paperdigest.db`)
Tables: `users`, `sessions`, `papers`, `user_papers` (join table with `is_read`, `is_starred`, `folder_id`), `folders`, `fetch_log`, `mail_log`. Schema migrations are additive ALTER TABLE statements in `app/db._migrate()`.

### Scopus quartile filtering
`fetch.py` loads `data/scopus.csv` (SCImago format, semicolon-delimited) at startup. Papers whose ISSN maps to Q3/Q4 are dropped when `q2_hard: true` in the user config. The CSV must be uploaded via `/admin` before filtering works. Refresh annually from https://www.scimagojr.com/journalrank.php (download the CSV, upload via `/admin`).

### Email digest logic
`email_digest.py` only includes papers that are **unactioned**: `is_read = 0`, `is_starred = 0`, `folder_id IS NULL`, and `added_at` within the last 24 hours. If the resulting list is empty and `email_suppress_empty: true`, no email is sent; if `false`, a "nothing new today" confirmation is sent. The two scripts are intentionally separate so the digest can be re-sent without re-fetching (useful after email failures).

### MeSH topic classification
Classification into user-defined topic labels happens at query time in the web app (not at fetch time), by matching `papers.mesh_terms` against the user's `mesh_topic_map`. This means users can update their topic map and immediately see reclassified results without re-fetching. Papers matching no MeSH term show as "Unclassified".

## Infrastructure

The app runs at whatever is set in `base_url` of `config.yaml`.
The FastAPI process is never exposed directly.

**Crontab entries** (user crontab for the `{user}` service account):
```
0  6 * * * /var/www/papersdaily/venv/bin/python /var/www/papersdaily/fetch.py >> /var/www/papersdaily/logs/fetch.log 2>&1
15 6 * * * /var/www/papersdaily/venv/bin/python /var/www/papersdaily/email_digest.py >> /var/www/papersdaily/logs/email.log 2>&1
```

## Sample files (public repo hygiene)

`config.yaml` and `users/*.yaml` contain secrets and personal data and are excluded from git. Their sanitised counterparts **must be kept in sync**:

- `config.yaml.sample` — mirrors every key in `config.yaml` with empty or safe default values; no real credentials, URLs, or personal data.
- `users/example.yaml.sample` — mirrors every key a user YAML can have; generic example values only.

**When you add, remove, or rename a config key** (in `config.yaml`, in user YAML handling code, or in `fetch.py`/`email_digest.py`), update the corresponding sample file in the same change. The sample files are the only documentation a new user has for what these files should look like.

Similarly, if the `.gitignore` needs updating (e.g. a new secret file is introduced), update it immediately.

## Design decisions

These explain why the code looks the way it does — don't change these patterns without good reason.

- **No LLM classification.** Topic classification is deterministic via `mesh_topic_map` only. LLMs introduce hallucination risk in a tool that must be reliable for scientific monitoring.
- **Auto-mark-as-read on paper open.** Visiting `/paper/<pmid>` marks the paper as read immediately (inside the same DB transaction as the page load). The paper page therefore always shows "Mark unread". The feed's toggle-read button still works for toggling without opening the full page.
- **RIS over BibTeX.** EndNote opens `.ris` files natively on double-click. BibTeX requires an import wizard. RIS is the correct choice for this user's workflow.
- **Cron over APScheduler.** Cron is transparent and decoupled from the FastAPI process lifecycle. APScheduler would couple fetch logic to the web process.
- **SQLite over Postgres.** Volume is ~10 papers/day for a handful of users. WAL mode is sufficient. Simplicity is the priority.
- **`<details>/<summary>` for email abstracts.** Works in Apple Mail, Thunderbird, Gmail web, Outlook web. Does not work in Outlook desktop — known and accepted trade-off.
- **No self-registration.** Admin creates all accounts via `/admin`. This is intentional for a closed personal tool.
- **No paywall bypass scripting.** institutional access uses Shibboleth/SAML SSO (browser-only). Unpaywall covers open-access PDFs. Paywall scripting would violate publisher terms.
- **Silent Scopus CSV overwrite.** No confirmation dialog on upload — intentional simpler UX for an admin action.
