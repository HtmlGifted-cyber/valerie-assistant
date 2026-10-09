"""
mail_router.py - picks where Valerie gets mail from, per account.

Sources:
  server       - live IMAP/SMTP (mail_client.py); needs that mailbox's
                 password in Settings > Mail.
  thunderbird  - Thunderbird's downloaded copy + its compose window
                 (thunderbird_mail.py); no password needed.

cfg["mail_source"]:
  "auto"        - server when a password is set and the server answers,
                  otherwise Thunderbird (the reply says which was used)
  "thunderbird" - Thunderbird only
  "server"      - server only

Ids carry their source so a later read/mark/delete goes to the same
place: "s123" = server UID 123, "t4" = Thunderbird message 4 (a bare
number is treated as Thunderbird, as in earlier versions).

Workspaces' mailboxes stay separate: every call names one workspace, and
replies are looked up in that same workspace only.
"""

import mail_client
import thunderbird_mail as tbmail

MailErrors = (mail_client.MailError, tbmail.MailError)

# (account, "s<uid>") -> Message-ID, so an email can still be found after it
# has been filed to Trash (where the server gives it a different UID).
_MID_CACHE = {}


def _server_ready(cfg, account):
    m = mail_client.account_settings(cfg, account)
    return bool(m.get("enabled") and m.get("password"))


def _order(cfg, account):
    mode = cfg.get("mail_source", "auto")
    if mode == "server":
        return ["server"]
    if mode == "thunderbird":
        return ["thunderbird"]
    return (["server"] if _server_ready(cfg, account) else []) + ["thunderbird"]


def _split_id(msg_id):
    msg_id = str(msg_id).strip()
    if msg_id[:1] in ("s", "S"):
        return "server", msg_id[1:]
    if msg_id[:1] in ("t", "T"):
        return "thunderbird", msg_id[1:]
    return "thunderbird", msg_id


def list_messages(cfg, account, folder="INBOX", unread_only=False, limit=10, search=None):
    """-> (source, messages, note). Messages have prefixed ids."""
    notes = []
    for src in _order(cfg, account):
        try:
            if src == "server":
                rows = mail_client.list_messages(cfg, folder, unread_only, limit, search, account=account)
                for r in rows:
                    r["id"] = "s" + r.pop("uid")
                    if r.get("message_id"):
                        _MID_CACHE[(account, r["id"])] = r["message_id"]
            else:
                rows = tbmail.list_messages(account, folder, unread_only, limit, search)
                for r in rows:
                    r["id"] = "t" + r["id"]
            return src, rows, " ".join(notes)
        except MailErrors as e:
            notes.append(f"({src} unavailable: {e} - trying the next source.)")
    raise mail_client.MailError(" ".join(notes) or "No mail source is available.")


def read_message(cfg, account, msg_id, folder="INBOX"):
    src, raw = _split_id(msg_id)
    if src == "server":
        try:
            m = mail_client.read_message(cfg, raw, folder, account=account)
        except mail_client.MailError:
            # already filed to Trash after a report - find it there by Message-ID
            mid = _MID_CACHE.get((account, "s" + raw))
            if not mid:
                raise
            trash = mail_client.account_settings(cfg, account).get("trash_folder", "Trash")
            tuid = mail_client.find_uid(cfg, mid, trash, account=account)
            m = mail_client.read_message(cfg, tuid, trash, account=account)
        m.setdefault("reply_to", m.get("from", ""))
    else:
        m = tbmail.read_message(account, raw, folder)
    m["source"] = src
    return m


def set_read(cfg, account, msg_id, read=True, folder="INBOX"):
    src, raw = _split_id(msg_id)
    if src == "thunderbird":
        if not _server_ready(cfg, account):
            tbmail.open_thunderbird()
            return ("I can't change mail inside Thunderbird directly - I've opened Thunderbird for you. "
                    "(Add this mailbox's server password in Settings > Mail for one-click changes.)")
        m = tbmail.read_message(account, raw, folder)
        raw = mail_client.find_uid(cfg, m["message_id"], folder, account=account)
    mail_client.set_read(cfg, raw, read, folder, account=account)
    return f"Marked {'read' if read else 'unread'} on the server."


def delete_message(cfg, account, msg_id, folder="INBOX"):
    src, raw = _split_id(msg_id)
    if src == "thunderbird":
        if not _server_ready(cfg, account):
            tbmail.open_thunderbird()
            return ("I can't delete inside Thunderbird directly - I've opened Thunderbird for you. "
                    "(Add this mailbox's server password in Settings > Mail for one-click delete.)")
        m = tbmail.read_message(account, raw, folder)
        raw = mail_client.find_uid(cfg, m["message_id"], folder, account=account)
    mail_client.delete_message(cfg, raw, folder, account=account)
    return "Moved to Trash on the server."


def send(cfg, account, to, subject, body, reply_to_id=None, confirm=None):
    """Server: Valerie's own Yes/No popup, then SMTP. Thunderbird: opens a
    filled-in compose window that the owner sends."""
    in_reply_to = None
    if reply_to_id:
        orig = read_message(cfg, account, reply_to_id)
        to = to or orig.get("reply_to") or orig.get("from")
        in_reply_to = orig.get("message_id")
        if not subject.lower().startswith("re:"):
            subject = "Re: " + orig["subject"]

    notes = []
    for src in _order(cfg, account):
        try:
            if src == "server":
                sender = mail_client.account_settings(cfg, account).get("address", "")
                preview = (f"FROM: {mail_client.account_label(cfg, account)} <{sender}>\n"
                           f"To: {to}\nSubject: {subject}\n\n{body}")
                if confirm is None:
                    return "Sending needs the owner's approval in the Valerie window; not sent."
                if not confirm("Send this email?", preview):
                    return "Declined - email NOT sent."
                mail_client.send_message(cfg, to, subject, body, in_reply_to, account=account)
                return f"Email sent to {to} from the {account} mailbox (via the mail server)."
            sender = tbmail.compose(account, to, subject, body, cfg=cfg)
            return (" ".join(notes) + f" Opened a Thunderbird compose window from {sender} to {to}; "
                    "nothing is sent until the owner clicks Send in Thunderbird.").strip()
        except MailErrors as e:
            notes.append(f"({src} unavailable: {e})")
    raise mail_client.MailError(" ".join(notes))
