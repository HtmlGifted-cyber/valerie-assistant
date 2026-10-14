"""va_notify.py - Reach the owner's phone (or desktop) when they're away, and get a short answer back.

Free and open source: ntfy (https://ntfy.sh, or a self-hosted ntfy server). The ntfy app is free on Android and iOS.

notify(title, message, priority="default", tags=None, urgent=False, click_url=None) -> str
                                        send an alert (ntfy phone, desktop pop-up, or nothing)
ask_owner(question, options=("Yes","No"), timeout_min=30, details="") -> str
                                        ask on the phone; each option is a tap button; waits for the answer
setup_notify(method="ntfy", server=None) -> str
                                        create the secret topics and save the notify settings
notify_status() -> str                  method, server, topics (masked), last log lines

Config lives under cfg["notify"] (see setup_notify). Log: DATA_DIR/notify.log (no secrets).
Test override: env VALERIE_NOTIFY_TEST_TOPIC=<topic> forces method ntfy for that process and uses
that topic (replies go to <topic>-r). The real config is not read for topics or written in that mode.

CLI: python va_notify.py setup [ntfy|desktop|none] | test | send "title" "message" | ask "question" [opt1 opt2 opt3] | status
"""
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

import requests

import config as cfg_mod

DEFAULT_SERVER = "https://ntfy.sh"
TEST_ENV = "VALERIE_NOTIFY_TEST_TOPIC"
METHODS = ("ntfy", "desktop", "none")
LOG_PATH = os.path.join(cfg_mod.DATA_DIR, "notify.log")
TIMEOUT = 10                   # seconds per web request
POLL_EVERY = 5                 # seconds between checks for a phone answer
MAX_OPTIONS = 3
PRIORITY_NAMES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}
UA = "Valerie-notify"

_SECRET_KEYVAL = re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)(\s*[:=]\s*)\S+")
_SECRET_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
_SECRET_PREFIX = re.compile(r"\b(sk-|nvapi-)[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{20,}")
_SECRET_LONG = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")


# ---------- helpers ----------

def _redact(text):
    """Hide things that look like passwords, API keys or tokens before anything leaves this laptop."""
    text = str(text if text is not None else "")
    text = _SECRET_KEYVAL.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    text = _SECRET_BEARER.sub(lambda m: m.group(1) + "[redacted]", text)
    text = _SECRET_PREFIX.sub("[redacted]", text)

    def long_one(m):
        s = m.group(0)
        return "[redacted]" if re.search(r"[A-Za-z]", s) and re.search(r"[0-9]", s) else s
    return _SECRET_LONG.sub(long_one, text)


def _log(kind, text):
    """Append one line to notify.log. Never pass topics, tokens or server addresses here."""
    try:
        os.makedirs(cfg_mod.DATA_DIR, exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        clean = " ".join(str(text).split())
        with open(LOG_PATH, "a") as f:
            f.write("%s\t%s\t%s\n" % (stamp, kind, clean))
    except OSError:
        pass


def _defaults():
    return {"method": "desktop", "ntfy_server": DEFAULT_SERVER, "topic": "", "reply_topic": "",
            "ntfy_token": "", "quiet_hours": None}


def _settings():
    """Current notify settings. The test env var overrides topics and forces ntfy."""
    cfg = cfg_mod.load_config()
    n = _defaults()
    n.update(cfg.get("notify") or {})
    n["ntfy_server"] = (n.get("ntfy_server") or DEFAULT_SERVER).strip().rstrip("/")
    test_topic = os.environ.get(TEST_ENV, "").strip()
    if test_topic:
        n["method"] = "ntfy"
        n["topic"] = test_topic
        n["reply_topic"] = test_topic + "-r"
    return n


def _new_topic():
    return "valerie-" + secrets.token_urlsafe(18)


def _headers(n):
    h = {"User-Agent": UA}
    if n.get("ntfy_token"):
        h["Authorization"] = "Bearer " + n["ntfy_token"]
    return h


def _priority(p, urgent=False):
    if isinstance(p, bool):
        p = 3
    if isinstance(p, int) and 1 <= p <= 5:
        v = p
    else:
        v = PRIORITY_NAMES.get(str(p).strip().lower(), 3)
    if urgent:
        v = max(v, 4)
    return v


def _minutes(hhmm):
    h, m = str(hhmm).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("bad time")
    return h * 60 + m


def _quiet_now(n):
    """True if the current time is inside cfg notify.quiet_hours (e.g. ["22:00", "07:00"])."""
    q = n.get("quiet_hours")
    if not q or not isinstance(q, (list, tuple)) or len(q) != 2:
        return False
    try:
        start, end = _minutes(q[0]), _minutes(q[1])
    except (ValueError, TypeError):
        return False
    if start == end:
        return False
    now = datetime.datetime.now()
    mins = now.hour * 60 + now.minute
    if start < end:
        return start <= mins < end
    return mins >= start or mins < end    # window crosses midnight


def _mask(topic):
    if not topic:
        return "not set"
    return topic[:10] + "... (hidden)"


# ---------- sending ----------

def _ntfy_send(n, title, message, prio, tags, click_url):
    if not n.get("topic"):
        return "Phone alerts aren't set up yet. Run: va_notify.py setup ntfy"
    body = {"topic": n["topic"], "title": title, "message": message, "priority": prio,
            "tags": list(tags or [])}
    if click_url:
        body["click"] = click_url
    try:
        r = requests.post(n["ntfy_server"], json=body, headers=_headers(n), timeout=TIMEOUT)
    except requests.RequestException as e:
        _log("failed", "ntfy send: %s" % type(e).__name__)
        return "Could not reach the phone alert server (%s)." % type(e).__name__
    if r.status_code == 200:
        _log("sent", "ntfy: %s | %s" % (title, message))
        return "Sent to your phone (HTTP 200)."
    _log("failed", "ntfy send: HTTP %d" % r.status_code)
    return "Phone alert failed: the server answered HTTP %d." % r.status_code


def _desktop_send(title, message, prio):
    if not shutil.which("notify-send"):
        return "Desktop alerts need notify-send, which isn't installed."
    urgency = "critical" if prio >= 4 else "normal"
    try:
        r = subprocess.run(["notify-send", "-u", urgency, title, message],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "The desktop alert timed out."
    if r.returncode != 0:
        _log("failed", "desktop send: exit %d" % r.returncode)
        return "The desktop alert failed: %s" % ((r.stderr or "").strip() or "unknown error")
    _log("sent", "desktop: %s | %s" % (title, message))
    return "Shown on this laptop."


def notify(title, message, priority="default", tags=None, urgent=False, click_url=None):
    n = _settings()
    method = n.get("method", "desktop")
    if method == "none":
        return "Alerts are off (method none), so nothing was sent."
    if method not in METHODS:
        return "Unknown alert method '%s'. Use ntfy, desktop or none." % method
    if not urgent and _quiet_now(n):
        _log("held", "quiet hours: %s" % _redact(title))
        return ("It's quiet hours right now, so this wasn't sent. "
                "Use urgent=True if it can't wait.")
    title = _redact(title)
    message = _redact(message)
    prio = _priority(priority, urgent)
    if method == "ntfy":
        return _ntfy_send(n, title, message, prio, tags, click_url)
    return _desktop_send(title, message, prio)


# ---------- asking the owner ----------

def _poll_for(n, rid, since):
    """Return the chosen option if a reply for this request id has arrived, else None."""
    url = "%s/%s/json?poll=1&since=%d" % (n["ntfy_server"], n["reply_topic"], since)
    try:
        r = requests.get(url, headers=_headers(n), timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    for line in r.text.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message") or ""
        if o.get("event") == "message" and msg.startswith(rid + "|"):
            return msg[len(rid) + 1:]
    return None


def ask_owner(question, options=("Yes", "No"), timeout_min=30, details=""):
    n = _settings()
    if n.get("method") != "ntfy":
        return ("Questions to your phone need the ntfy method. Run: va_notify.py setup ntfy "
                "(the method is now '%s')." % n.get("method"))
    if not n.get("topic") or not n.get("reply_topic"):
        return "Phone alerts aren't set up yet. Run: va_notify.py setup ntfy"
    opts = [str(o).strip() for o in (options or ()) if str(o).strip()][:MAX_OPTIONS] or ["Yes", "No"]
    question = _redact(question)
    details = _redact(details)
    message = question + ("\n" + details if details else "")
    rid = secrets.token_urlsafe(6)
    reply_url = "%s/%s" % (n["ntfy_server"], n["reply_topic"])
    actions = [{"action": "http", "label": o, "url": reply_url, "method": "POST",
                "body": "%s|%s" % (rid, o), "clear": True} for o in opts]
    body = {"topic": n["topic"], "title": "Question from your assistant", "message": message,
            "priority": 4, "tags": ["question"], "actions": actions}
    start = int(time.time())
    print("[ask] request id: %s" % rid, file=sys.stderr, flush=True)
    try:
        r = requests.post(n["ntfy_server"], json=body, headers=_headers(n), timeout=TIMEOUT)
    except requests.RequestException as e:
        _log("failed", "ask send: %s" % type(e).__name__)
        return "Could not send the question to your phone (%s)." % type(e).__name__
    if r.status_code != 200:
        _log("failed", "ask send: HTTP %d" % r.status_code)
        return "Could not send the question to your phone: the server answered HTTP %d." % r.status_code
    _log("asked", "%s | options: %s" % (question, ", ".join(opts)))
    deadline = time.time() + float(timeout_min) * 60
    while True:
        answer = _poll_for(n, rid, start)
        if answer is not None:
            _log("answered", "%s | %s" % (question, answer))
            return "Owner answered: %s" % answer
        if time.time() >= deadline:
            break
        time.sleep(min(POLL_EVERY, max(0.0, deadline - time.time())))
    _log("no answer", question)
    return "No answer within %g minutes." % timeout_min


# ---------- setup and status ----------

def setup_notify(method="ntfy", server=None):
    method = (method or "ntfy").strip().lower()
    if method not in METHODS:
        return "Unknown method '%s'. Choose ntfy, desktop or none." % method
    if os.environ.get(TEST_ENV):
        return "Test mode is on (%s is set), so the real config was not changed." % TEST_ENV
    cfg = cfg_mod.load_config()
    n = _defaults()
    n.update(cfg.get("notify") or {})
    srv = (server or n.get("ntfy_server") or DEFAULT_SERVER).strip().rstrip("/")
    if not re.match(r"^https?://\S+$", srv):
        return "The server address must start with https:// (or http:// for a server on your own network)."
    n["method"] = method
    n["ntfy_server"] = srv
    if not n.get("topic"):
        n["topic"] = _new_topic()
    if not n.get("reply_topic"):
        n["reply_topic"] = _new_topic()
    cfg["notify"] = n
    cfg_mod.save_config(cfg)
    _log("setup", "method=%s server=%s" % (method, srv))
    if method == "ntfy":
        lines = [
            "Phone alerts are set up with ntfy (free app on Android and iOS).",
            "1. Install the ntfy app.",
            "2. Tap + and subscribe to this link (plain text, copy it exactly):",
            "   %s/%s" % (srv, n["topic"]),
            "3. Keep that link secret. Anyone who knows it can read your alerts.",
            "Replies from your phone use a second private topic that is never shown here.",
        ]
        if srv != DEFAULT_SERVER:
            lines.insert(2, "   (You use your own server, so add %s in the app's settings first.)" % srv)
        return "\n".join(lines)
    if method == "desktop":
        return "Alerts will show as desktop pop-ups (notify-send). Phone questions need the ntfy method."
    return "Alerts are off. Nothing will be sent."


def notify_status():
    n = _settings()
    lines = ["Alert method: %s%s" % (n.get("method"), " (test mode, set by %s)" % TEST_ENV
                                     if os.environ.get(TEST_ENV) else ""),
             "Server: %s" % n.get("ntfy_server"),
             "Phone topic: %s" % _mask(n.get("topic")),
             "Reply topic: %s" % _mask(n.get("reply_topic")),
             "Access token: %s" % ("set" if n.get("ntfy_token") else "not set"),
             "Quiet hours: %s" % ("%s to %s" % tuple(n["quiet_hours"])
                                  if n.get("quiet_hours") and len(n["quiet_hours"]) == 2 else "off")]
    recent = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            recent = [l.rstrip("\n") for l in f.readlines()[-5:]]
    lines.append("Last 5 log lines:")
    lines += ["  " + _redact(l) for l in recent] or ["  (no log yet)"]
    return "\n".join(lines)


# ---------- command line ----------

USAGE = ('Usage: va_notify.py setup [ntfy|desktop|none] | test | send "title" "message" | '
         'ask "question" [opt1 opt2 opt3] | status')


def main(argv):
    if not argv:
        print(USAGE)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "setup":
        print(setup_notify(args[0] if args else "ntfy"))
    elif cmd == "test":
        print(notify("Valerie test", "If you can read this, phone alerts work.", tags=["white_check_mark"]))
    elif cmd == "send" and len(args) >= 2:
        print(notify(args[0], args[1]))
    elif cmd == "ask" and len(args) >= 1:
        print(ask_owner(args[0], tuple(args[1:]) or ("Yes", "No")))
    elif cmd == "status":
        print(notify_status())
    else:
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
