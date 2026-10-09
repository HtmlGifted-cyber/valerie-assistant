"""
thunderbird_mail.py - Valerie's mail backend that works THROUGH the local
Thunderbird client instead of logging in to the mail server herself.

  - Reading: parses the mail Thunderbird has already downloaded into its
    profile (ImapMail/<server>/<folder> mbox files). Read-only - these
    files belong to Thunderbird and are never written to.
  - Sending / replying: opens a pre-filled Thunderbird compose window
    from the right account; the owner reviews it and clicks Send.
  - No passwords are needed or stored - Thunderbird keeps those.

Each workspace's mailbox is a separate identity and is never mixed:
every call names one workspace. The matching Thunderbird account is found
by the workspace's email address.
"""

import configparser
import email
import email.header
import email.utils
import mailbox
import os
import re
import shutil
import subprocess
import tempfile

TB_ROOT = os.path.expanduser("~/.thunderbird")

STATUS_READ = 0x0001
STATUS_DELETED = 0x0008


class MailError(Exception):
    pass


def _dh(value):
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def profile_dir():
    ini = os.path.join(TB_ROOT, "profiles.ini")
    cp = configparser.ConfigParser()
    cp.read(ini)
    # The profile Thunderbird actually starts with is in the [Install...] section.
    for sec in cp.sections():
        if sec.startswith("Install") and cp.has_option(sec, "Default"):
            return os.path.join(TB_ROOT, cp.get(sec, "Default"))
    for sec in cp.sections():
        if cp.get(sec, "Default", fallback="") == "1" and cp.has_option(sec, "Path"):
            return os.path.join(TB_ROOT, cp.get(sec, "Path"))
    raise MailError("Couldn't find the Thunderbird profile.")


def _tb_servers():
    """{email/username (lower): server folder relative to the profile} from
    Thunderbird's prefs.js (account names only - passwords are never read)."""
    prefs = os.path.join(profile_dir(), "prefs.js")
    servers = {}
    try:
        with open(prefs, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return servers
    fields = {}
    for key, val in re.findall(r'user_pref\("mail\.server\.(server\d+\.(?:userName|name|directory-rel|type))",\s*"([^"]*)"\);', text):
        sid, field = key.split(".", 1)
        fields.setdefault(sid, {})[field] = val
    for sid, f in fields.items():
        if f.get("type") not in ("imap", "pop3"):
            continue
        rel = f.get("directory-rel", "").replace("[ProfD]", "")
        for ident in (f.get("userName", ""), f.get("name", "")):
            if ident:
                servers[ident.lower()] = rel
    return servers


def _account(account, cfg=None):
    """-> (Thunderbird server folder, sending address) for a workspace."""
    import config as cfg_mod
    import workspaces
    cfg = cfg or cfg_mod.load_config()
    if account not in workspaces.all_ids(cfg):
        raise MailError(f"Unknown mailbox '{account}'.")
    m = workspaces.mail(cfg, account)
    address = (m.get("address") or "").lower()
    if not address:
        raise MailError(f"No email address set for the {workspaces.label(cfg, account)} workspace (Settings > Workspaces).")
    servers = _tb_servers()
    rel = servers.get(address) or servers.get((m.get("username") or "").lower())
    if not rel:
        raise MailError(f"Thunderbird has no account for {address}.")
    return rel, m.get("address")


def _folder_path(account, folder):
    sub, _addr = _account(account)
    base = os.path.join(profile_dir(), sub)
    # Thunderbird stores subfolders as "<Parent>.sbd/<Child>".
    parts = [p for p in re.split(r"[/\\]", folder or "INBOX") if p]
    path = os.path.join(base, *[p + ".sbd" for p in parts[:-1]], parts[-1])
    if not os.path.isfile(path):
        raise MailError(f"Thunderbird has no downloaded copy of '{folder}' for the {account} mailbox. "
                        "In Thunderbird: right-click the folder > Properties > Synchronization > "
                        "'Select this folder for offline use', or open the folder once.")
    return path


def list_folders(account):
    sub, _ = _account(account)
    base = os.path.join(profile_dir(), sub)
    out = []
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            full = os.path.join(dirpath, f)
            if "." in f or os.path.getsize(full) == 0:
                continue
            rel = os.path.relpath(full, base).replace(".sbd" + os.sep, "/")
            out.append(rel)
    return sorted(out)


def _messages(account, folder):
    mb = mailbox.mbox(_folder_path(account, folder), create=False)
    try:
        for key, msg in mb.iteritems():
            try:
                status = int(msg.get("X-Mozilla-Status", "0") or "0", 16)
            except ValueError:
                status = 0
            if status & STATUS_DELETED:
                continue
            yield key, msg, status
    finally:
        mb.close()


def list_messages(account, folder="INBOX", unread_only=False, limit=10, search=None):
    rows = []
    needle = (search or "").lower()
    for key, msg, status in _messages(account, folder):
        unread = not (status & STATUS_READ)
        if unread_only and not unread:
            continue
        if needle:
            hay = f"{msg.get('From', '')} {msg.get('Subject', '')}".lower()
            if needle not in _dh(hay).lower() and needle not in _body_text(msg)[:4000].lower():
                continue
        rows.append({"id": str(key), "unread": unread, "from": _dh(msg.get("From")),
                     "subject": _dh(msg.get("Subject")), "date": _dh(msg.get("Date"))[:31]})
    return rows[::-1][:int(limit)]


def _body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "ignore")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "ignore")
                import mail_format
                return mail_format.html_to_text(html)
        return ""
    text = (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "ignore")
    if msg.get_content_type() == "text/html":
        import mail_format
        text = mail_format.html_to_text(text)
    return text


def read_message(account, msg_id, folder="INBOX", max_chars=5000):
    for key, msg, _status in _messages(account, folder):
        if str(key) == str(msg_id):
            body = _body_text(msg).strip()
            return {"id": str(key), "from": _dh(msg.get("From")), "to": _dh(msg.get("To")),
                    "subject": _dh(msg.get("Subject")), "date": _dh(msg.get("Date")),
                    "reply_to": _dh(msg.get("Reply-To") or msg.get("From")),
                    "message_id": msg.get("Message-ID", ""),
                    "body": body[:max_chars] + ("\n...[truncated]" if len(body) > max_chars else "")}
    raise MailError(f"No email with id {msg_id} in the {account} {folder} folder.")


def _tb_value(v):
    # Thunderbird's -compose syntax: key='value',key='value'
    return "'" + str(v).replace("'", "’") + "'"


def compose(account, to, subject, body, cfg=None):
    """Open a pre-filled Thunderbird compose window from the given account.
    Nothing is sent until the owner clicks Send in Thunderbird."""
    _sub, sender = _account(account, cfg)
    tb = shutil.which("thunderbird")
    if not tb:
        raise MailError("Thunderbird isn't installed.")
    fd, body_path = tempfile.mkstemp(prefix="valerie-mail-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    spec = ",".join([f"from={_tb_value(sender)}", f"to={_tb_value(to)}",
                     f"subject={_tb_value(subject)}", f"message={_tb_value(body_path)}"])
    subprocess.Popen([tb, "-compose", spec], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return sender


def open_thunderbird():
    tb = shutil.which("thunderbird")
    if tb:
        subprocess.Popen([tb, "-mail"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def test(account):
    folders = list_folders(account)
    unread = len(list_messages(account, "INBOX", unread_only=True, limit=500))
    total = len(list_messages(account, "INBOX", limit=5000))
    return f"{account}: {total} emails downloaded in INBOX ({unread} unread). Folders on disk: {', '.join(folders) or 'none'}"
