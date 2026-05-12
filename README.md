# Papers Daily

A self-hosted scientific literature monitor. Fetches papers from PubMed, filters by Scopus journal quartile, and delivers daily email digests. Multi-user, with per-user PubMed search profiles.

## Requirements

- Python 3.11+
- A free [NCBI API key](https://www.ncbi.nlm.nih.gov/account/)
- An SMTP relay (e.g. Gmail App Password)

## Setup

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Copy and edit the config
cp config.yaml.sample config.yaml
# Fill in ncbi_api_key, ncbi_email, smtp_* fields

# 3. Create a user config (one file per user)
cp users/example.yaml.sample users/<username>.yaml
# Edit to set email and search_profiles

# 4. Create the initial admin account
venv/bin/python setup_admin.py <username> <password>

# 5. Upload the Scopus CSV via the web UI (/admin) for quartile filtering
#    Download from: https://www.scimagojr.com/journalrank.php

# 6. Run the app
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 2711 --reload
```

## Cron

```
0  6 * * * /path/to/venv/bin/python /path/to/fetch.py >> /path/to/logs/fetch.log 2>&1
15 6 * * * /path/to/venv/bin/python /path/to/email_digest.py >> /path/to/logs/email.log 2>&1
```

## Deploy as a systemd service

```bash
# Edit papersdaily.service to match your paths and user, then:
sudo cp papersdaily.service /etc/systemd/system/
sudo systemctl enable --now papersdaily
```

See `CLAUDE.md` for full architecture and design notes.
