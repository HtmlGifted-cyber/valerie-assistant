"""
mail_client.py - Valerie's mailbox connector (IMAP + SMTP, stdlib only).

Talks directly to the mail server for one workspace's mailbox at a time;
workspaces never share or mix mail.

Safety:
  - Reading uses BODY.PEEK, so just reading never marks a message read;
    marking read/unread is its own explicit action.
  - "Delete" moves the message to Trash (recoverable), never expunges
    from Trash.
  - Sending requires an interactive confirmation (tools.CONFIRM); the
    headless scheduler has no confirmer, so it can never send.

Messages are addressed by IMAP UID, which stays stable between calls.
"""

import email
import email.header
import email.utils
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage

TIMEOUT = 30


class MailError(Exception):
    pass


def _dh(value):
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def account_settings(cfg, account):
    import workspaces
    return workspaces.mail(cfg, account)


def account_label(cfg, account):
    import workspaces
    m = account_settings(cfg, account)
    return f"{workspaces.label(cfg, account).upper()}" + (f" ({m.get('address')})" if m.get("address") else "")


def _settings(cfg, account=None):
    """Each workspace's mailbox is a separate identity and is never mixed:
    every call names exactly one workspace."""
    import workspaces
    if account not in workspaces.all_ids(cfg):
        raise MailError(f"Unknown mailbox '{account}'. Workspaces: {', '.join(workspaces.all_ids(cfg)) or 'none set up'}.")
    m = account_settings(cfg, account)
    if not m.get("enabled"):
        raise MailError(f"The {account} mailbox isn't set up yet - open Settings > Mail, fill it in and tick 'Enable'.")
    if not (m.get("address") and m.get("password") and m.get("imap_host")):
        raise MailError("Mail settings are incomplete (address, password and IMAP server are required).")
    return m


def _imap(m):
    import socket
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(TIMEOUT)  # Python 3.8's IMAP4_SSL has no timeout argument
    try:
        conn = imaplib.IMAP4_SSL(m["imap_host"], int(m.get("imap_port", 993)),
                                 ssl_context=ssl.create_default_context())
        conn.login(m.get("username") or m["address"], m["password"])
        return conn
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailError(f"Couldn't log in to {m['imap_host']}: {e}")
    finally:
        socket.setdefaulttimeout(old)


def _quote(folder):
    return '"' + folder.replace('"', '\\"') + '"'


def _select(conn, folder, readonly=True):
    typ, _ = conn.select(_quote(folder), readonly=readonly)
    if typ != "OK":
        raise MailError(f"Folder '{folder}' not found.")


def _body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                import mail_format
                return mail_format.html_to_text(html)
        return ""
    payload = msg.get_payload(decode=True) or b""
    text = payload.decode(msg.get_content_charset() or "utf-8", "ignore")
    if msg.get_content_type() == "text/html":
        import mail_format
        text = mail_format.html_to_text(text)
    return text


def list_folders(cfg, account=None):
    conn = _imap(_settings(cfg, account))
    try:
        _, data = conn.list()
        names = []
        for line in data or []:
            s = line.decode(errors="ignore")
            m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"(\S+)\s*$", s)
            if m:
                names.append(m.group(1))
        return names
    finally:
        conn.logout()


def list_messages(cfg, folder="INBOX", unread_only=False, limit=10, search=None, account=None):
    m = _settings(cfg, account)
    conn = _imap(m)
    try:
        _select(conn, folder)
        criteria = ["UNSEEN"] if unread_only else ["ALL"]
        if search:
            criteria = criteria + ["TEXT", '"' + search.replace('"', "") + '"']
        typ, data = conn.uid("SEARCH", None, *criteria)
        uids = (data[0] or b"").split()[-int(limit):][::-1]
        out = []
        for uid in uids:
            _, fd = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            flags = fd[0][0].decode(errors="ignore") if fd and fd[0] else ""
            hdr = email.message_from_bytes(fd[0][1]) if fd and isinstance(fd[0], tuple) else {}
            out.append({
                "uid": uid.decode(),
                "unread": "\\Seen" not in flags,
                "from": _dh(hdr.get("From")),
                "subject": _dh(hdr.get("Subject")),
                "date": _dh(hdr.get("Date"))[:31],
                "message_id": (hdr.get("Message-ID") or "").strip(),
            })
        return out
    finally:
        conn.logout()


def read_message(cfg, uid, folder="INBOX", max_chars=5000, account=None):
    m = _settings(cfg, account)
    conn = _imap(m)
    try:
        _select(conn, folder)
        _, fd = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if not fd or not isinstance(fd[0], tuple):
            raise MailError(f"No message with id {uid} in {folder}.")
        msg = email.message_from_bytes(fd[0][1])
        body = _body_text(msg).strip()
        return {
            "uid": str(uid), "from": _dh(msg.get("From")), "to": _dh(msg.get("To")),
            "subject": _dh(msg.get("Subject")), "date": _dh(msg.get("Date")),
            "message_id": msg.get("Message-ID", ""),
            "body": body[:max_chars] + ("\n...[truncated]" if len(body) > max_chars else ""),
        }
    finally:
        conn.logout()


def find_uid(cfg, message_id, folder="INBOX", account=None):
    """Map a Message-ID (from Thunderbird's copy) to the server's IMAP UID."""
    conn = _imap(_settings(cfg, account))
    try:
        _select(conn, folder)
        typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", '"' + message_id.strip("<>") + '"')
        uids = (data[0] or b"").split() if typ == "OK" else []
        if not uids:
            raise MailError("Couldn't find that email on the server (it may already be moved or deleted).")
        return uids[-1].decode()
    finally:
        conn.logout()


def set_read(cfg, uid, read=True, folder="INBOX", account=None):
    conn = _imap(_settings(cfg, account))
    try:
        _select(conn, folder, readonly=False)
        typ, _ = conn.uid("STORE", str(uid), "+FLAGS" if read else "-FLAGS", "(\\Seen)")
        if typ != "OK":
            raise MailError(f"Couldn't update message {uid}.")
    finally:
        conn.logout()


def delete_message(cfg, uid, folder="INBOX", account=None):
    """Move to Trash (recoverable). Falls back to flag-as-deleted + expunge
    of just this message only if the server has no Trash folder."""
    m = _settings(cfg, account)
    trash = m.get("trash_folder", "Trash")
    conn = _imap(m)
    try:
        _select(conn, folder, readonly=False)
        if folder != trash:
            typ, _ = conn.uid("COPY", str(uid), _quote(trash))
            if typ != "OK":
                raise MailError(f"Couldn't move message {uid} to '{trash}' (check the Trash folder name in Settings).")
        conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        conn.expunge()
    finally:
        conn.logout()


def file_batch(cfg, account, trash_uids=(), read_uids=(), folder="INBOX"):
    """One connection: mark read_uids + trash_uids as read, then move
    trash_uids to Trash (recoverable). Returns the Message-IDs trashed."""
    m = _settings(cfg, account)
    trash = m.get("trash_folder", "Trash")
    conn = _imap(m)
    moved = []
    try:
        _select(conn, folder, readonly=False)
        everything = [str(u) for u in list(trash_uids) + list(read_uids)]
        if everything:
            conn.uid("STORE", ",".join(everything), "+FLAGS", "(\\Seen)")
        if trash_uids:
            ids = ",".join(str(u) for u in trash_uids)
            _, data = conn.uid("FETCH", ids, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for part in data or []:
                if isinstance(part, tuple):
                    mid = email.message_from_bytes(part[1]).get("Message-ID", "").strip()
                    if mid:
                        moved.append(mid)
            typ, _ = conn.uid("COPY", ids, _quote(trash))
            if typ != "OK":
                raise MailError(f"Couldn't move messages to '{trash}'.")
            conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
            conn.expunge()
    finally:
        conn.logout()
    return moved


def restore_from_trash(cfg, account, message_ids, folder="INBOX"):
    """Undo: move these Message-IDs from Trash back to the inbox."""
    m = _settings(cfg, account)
    trash = m.get("trash_folder", "Trash")
    conn = _imap(m)
    back = 0
    try:
        _select(conn, trash, readonly=False)
        uids = []
        for mid in message_ids:
            typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", '"' + mid.strip("<>") + '"')
            if typ == "OK" and data and data[0]:
                uids += data[0].split()
        if uids:
            ids = ",".join(u.decode() for u in uids)
            conn.uid("COPY", ids, _quote(folder))
            conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            back = len(uids)
    finally:
        conn.logout()
    return back


def send_message(cfg, to, subject, body, in_reply_to=None, account=None):
    m = _settings(cfg, account)
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr((m.get("from_name", ""), m["address"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=m["address"].split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    host, port = m.get("smtp_host") or m["imap_host"], int(m.get("smtp_port", 465))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        server.login(m.get("username") or m["address"], m["password"])
        server.send_message(msg)
        server.quit()
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Sending failed: {e}")

    # Keep a copy in Sent, like a normal mail client would.
    try:
        conn = _imap(m)
        conn.append(_quote(m.get("sent_folder", "Sent")), "(\\Seen)", None, msg.as_bytes())
        conn.logout()
    except Exception:
        pass
    return msg["Message-ID"]


def test(cfg, account=None):
    folders = list_folders(cfg, account)
    unread = len(list_messages(cfg, "INBOX", unread_only=True, limit=200, account=account))
    return f"Connected. {unread} unread in INBOX. Folders: {', '.join(folders[:12])}"
