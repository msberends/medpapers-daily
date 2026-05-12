# Papers Daily

A self-hosted scientific literature monitor. Fetches papers from PubMed, filters by Scopus journal quartile, and delivers daily email digests. Multi-user, with per-user PubMed search profiles.

## Requirements

- Python 3.11+

Preferred:
- A free [NCBI API key](https://www.ncbi.nlm.nih.gov/account/)
- An SMTP relay (e.g. Gmail App Password)

## Setup

```bash
# 1. Create a virtual environment and install dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Create initial admin user
venv/bin/python setup_admin.py <username> <password>

# 3. Run the app
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 2711 --reload
```

## Cron

```
0  6 * * * /path/to/venv/bin/python /path/to/fetch.py && /path/to/venv/bin/python /path/to/email_digest.py
```

## Deploy as a systemd service

```bash
# Edit papersdaily.service to match your paths and user, then:
sudo cp papersdaily.service /etc/systemd/system/
sudo systemctl enable --now papersdaily
```

See `CLAUDE.md` for full architecture and design notes.
