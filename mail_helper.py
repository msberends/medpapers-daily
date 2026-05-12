"""
Shared email-sending helper for fetch.py and email_digest.py.
Supports sendmail relay and SMTP (Gmail app password).
"""
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr


def _from_header(config: dict) -> str:
    addr = config.get("email_from", "")
    name = config.get("email_from_name", "").strip()
    return formataddr((name, addr)) if name else addr


def send_email(config: dict, to: str, subject: str,
               html: str, plain: str):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = _from_header(config)
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    _dispatch(config, msg)


def send_plain_email(config: dict, to: str, subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = _from_header(config)
    msg["Subject"] = subject
    _dispatch(config, msg)


def _dispatch(config: dict, msg):
    relay = config.get("email_relay", "sendmail")
    if relay == "smtp":
        host = config.get("smtp_host", "smtp.gmail.com")
        port = int(config.get("smtp_port", 587))
        user = config.get("smtp_user", "")
        password = config.get("smtp_password", "")
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    else:
        subprocess.run(
            ["sendmail", "-t"],
            input=msg.as_string(),
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
