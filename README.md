# MedPapers Daily

A self-hosted monitor for medical scientific literature. This webapp fetches papers from PubMed daily and provides:

- Daily HTML email digests per user
- Web feed for browsing, searching, and filtering papers
- Scopus journal quartile filtering (Q1–Q4)
- MeSH-based topic classification
- Starring, read/unread tracking, folders, and relevance rating
- RIS export (for EndNote and reference managers)
- Open-access links via Unpaywall and institutional proxy support
- Per-user PubMed search profiles
- Multi-user support with an admin panel

## Security

MedPapers Daily is a closed, admin-provisioned tool — there is no self-registration. The following hardening measures are built in.

### Authentication & sessions

- **Constant-time login.** A dummy bcrypt hash is always checked when a username is not found, so valid and invalid usernames produce identical response times and cannot be enumerated by timing.
- **Brute-force protection.** Failed login attempts are tracked per IP and per username. Accounts are locked and a back-off delay is applied after repeated failures within a rolling window.
- **CSRF mitigation.** The session cookie is set with `SameSite=Lax`, which browsers enforce for all cross-site form submissions and navigations, making classic CSRF attacks ineffective.
- **Session invalidation on password change.** Changing a password immediately deletes all other active sessions from the database — a stolen session cookie cannot be used after the victim changes their password.
- **SHA-256 session tokens.** Raw tokens are never stored in the database; only their SHA-256 digest is kept. Sessions expire after 90 days.

### Injection & XSS prevention

- **Paper titles are HTML-sanitised.** PubMed titles can contain inline HTML (`<sub>`, `<sup>`, etc.). These are allowed through an `nh3`-based allow-list filter (`safe_title`); all other tags and attributes are stripped before rendering.
- **Admin-configured display names are escaped.** Publisher short names in `config.yaml` are HTML-escaped inside the `publisher_display` Jinja2 filter before the result is marked safe, so no admin-supplied string can inject HTML to other users.
- **SQL sort clauses use a dict lookup.** The feed sort parameter resolves to a hard-coded SQL fragment via a fixed mapping — there is no string interpolation from user input into SQL.
- **LIKE wildcards are escaped.** `%` and `_` characters in feed search terms are escaped before being passed to SQLite `LIKE` queries, preventing both wildcard injection and full-table-scan surprises.

### HTTP security headers

Every response carries a full set of security headers via a Starlette middleware:

| Header | Value |
|---|---|
| `Content-Security-Policy` | Restricts scripts, styles, images, and frames to trusted sources |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | Camera, microphone, and geolocation disabled |

### Deployment posture

- **Never exposed directly.** The FastAPI process listens on `127.0.0.1` only. All production traffic must go through a reverse proxy (nginx, Caddy, etc.) that handles TLS.
- **Dedicated service account.** The systemd unit runs as an unprivileged user with no sudo access; the app directory is its only writable path.
- **No paywall bypass.** Institutional access relies on Shibboleth/SAML (browser-only). Scripting around publisher paywalls would violate publisher terms and is intentionally absent.

## Requirements

- Python 3.12 (see `.python-version`; bcrypt 5.x is incompatible with Python 3.11)

Preferred:
- A free [NCBI API key](https://www.ncbi.nlm.nih.gov/account/) to increase the PubMed rate limit to 10 req/sec
- A free [Elsevier API key](https://dev.elsevier.com/) to retrieve Journal rankings
- An SMTP relay (e.g. Gmail App Password)

## Setup

```bash
# 1. Create a virtual environment and install dependencies
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Create initial admin user
venv/bin/python setup_admin.py <username> <password>

# 3. Run the app
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 2711 --reload
```

> All options can be set in the app and there is never a need to edit `.yaml` files manually.

## Cron

Use Cron to fetch new papers daily, and send a per-user email digest of new papers.

```
0  6 * * * /path/to/medpapers-daily/venv/bin/python /path/to/medpapers-daily/fetch.py && /path/to/medpapers-daily/venv/bin/python /path/to/medpapers-daily/email_digest.py
# or only fetch, without email
0  6 * * * /path/to/medpapers-daily/venv/bin/python /path/to/medpapers-daily/fetch.py
```

## Deploy as a systemd service

Run MedPapers Daily as a service to keep the webserver alive. A reverse proxy is a very effective way to get the app as one of your subdomains.

```bash
cp medpapers-daily.service.example medpapers-daily.service
# Edit medpapers-daily.service: replace YOUR_USERNAME and /path/to/medpapers-daily
nano medpapers-daily.service

sudo cp medpapers-daily.service /etc/systemd/system/
sudo systemctl enable --now medpapers-daily
```

## Screenshots

### Main Screen: Feed

![Feed](static/screenshots/feed.jpg)

### Journal Rankings

These rankings can be retrieved using an API from Elsevier. Instructions are provided in the app.

![Journals](static/screenshots/journals.jpg)

### Search Profiles

![Profiles](static/screenshots/profiles.jpg)

### Topics (MeSH mapping)

![Topics](static/screenshots/topics.jpg)

### Admin Menu

![Admin](static/screenshots/admin.jpg)

----

This project is licensed under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html).
