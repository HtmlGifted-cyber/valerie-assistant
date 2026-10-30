"""
email_connect.py - IMAP + SMTP email connector for Valerie.
No Google API keys required. Uses standard library imaplib & smtplib.
Settings come from ~/.config/valerie-assistant/email.json.
"""

import json
import os
import imaplib
import smtplib
import email
from email.message import EmailMessage

CONFIG_DIR = os.path.expanduser("~/.config/valerie-assistant")
EMAIL_CONFIG_PATH = os.path.join(CONFIG_DIR, "email.json")
EXAMPLE_CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "email.example.json"))

class EmailError(Exception):
    pass


def load_email_config():
    if not os.path.exists(EMAIL_CONFIG_PATH):
        raise EmailError(
            f"Email config not found at {EMAIL_CONFIG_PATH}. "
            "Please copy email.example.json to ~/.config/valerie-assistant/email.json and fill in your credentials."
        )
    try:
        with open(EMAIL_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        raise EmailError(f"Failed to parse email config JSON: {e}")

    required = ["imap_host", "imap_port", "smtp_host", "smtp_port", "address", "app_password"]
    for field in required:
        val = cfg.get(field, "")
        if not val or "YOUR_" in str(val) or "placeholder" in str(val).lower():
            raise EmailError(
                f"Email config at {EMAIL_CONFIG_PATH} contains placeholder or empty value for '{field}'. "
                "Please configure real credentials before connecting."
            )
    return cfg


def list_unread(n=10):
    """List up to n unread messages from INBOX."""
    cfg = load_email_config()
    host = cfg["imap_host"]
    port = int(cfg["imap_port"])
    address = cfg["address"]
    password = cfg["app_password"]

    try:
        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(address, password)
        mail.select("INBOX")

        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK':
            mail.logout()
            return []

        msg_ids = messages[0].split()
        # Sort or take last n
        recent_ids = msg_ids[-n:] if len(msg_ids) > n else msg_ids

        results = []
        for mid in recent_ids:
            # Use BODY.PEEK to avoid marking read
            res, data = mail.fetch(mid, '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])')
            if res != 'OK':
                continue

            raw_header = b""
            for response_part in data:
                if isinstance(response_part, tuple):
                    raw_header += response_part[1]

            msg = email.message_from_bytes(raw_header)
            subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
            sender = str(email.header.make_header(email.header.decode_header(msg.get("From", ""))))
            date = msg.get("Date", "")

            results.append({
                "id": mid.decode("utf-8") if isinstance(mid, bytes) else str(mid),
                "subject": subject,
                "from": sender,
                "date": date
            })

        mail.logout()
        return results
    except Exception as e:
        raise EmailError(f"IMAP error: {e}")


def send_mail(to, subject, body):
    """Send an email via SMTP (SSL or STARTTLS)."""
    cfg = load_email_config()
    smtp_host = cfg["smtp_host"]
    smtp_port = int(cfg["smtp_port"])
    address = cfg["address"]
    password = cfg["app_password"]

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = to

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)
            server.login(address, password)
            server.send_message(msg)
            server.quit()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
            server.login(address, password)
            server.send_message(msg)
            server.quit()
        return True
    except Exception as e:
        raise EmailError(f"SMTP error: {e}")
