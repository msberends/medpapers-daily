# Papers Daily

A self-hosted scientific interactive literature monitor.

It fetches papers from PubMed on a daily basis and sends them to email, with filters by Scopus journal quartile.

Multi-user, with per-user PubMed search profiles.

## Requirements

- Python 3.11+

Preferred:
- A free [NCBI API key](https://www.ncbi.nlm.nih.gov/account/) to increase the PubMed rate limit to 10 req/sec
- A free [Elsevier API key](https://dev.elsevier.com/) to retrieve Journal rankings
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

> All options can be set in the app and there is never a need to edit `.yaml` files manually.

## Cron

Use Cron to fetch new papers daily, and send a per-user email digest of new papers.

```
0  6 * * * /path/to/papersdaily/venv/bin/python /path/to/papersdaily/fetch.py && /path/to/papersdaily/venv/bin/python /path/to/papersdaily/email_digest.py
# or only fetch, without email
0  6 * * * /path/to/papersdaily/venv/bin/python /path/to/papersdaily/fetch.py
```

## Deploy as a systemd service

Run Papers Daily as a service to keep the webserver alive. A reverse proxy is a very effective way to get the app as one of your subdomains.

```bash
cp papersdaily.service.example papersdaily.service
# Edit papersdaily.service: replace YOUR_USERNAME and /path/to/papersdaily
nano papersdaily.service

sudo cp papersdaily.service /etc/systemd/system/
sudo systemctl enable --now papersdaily
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
