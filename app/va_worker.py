"""va_worker.py - client for an optional "edge AI worker" running on another device on the home network.

The worker is a small HTTP service (for example on a spare phone or mini-PC) that can report on other
machines and answer questions with its own local AI. Valerie only talks to it when the owner has turned it on:

    "edge_worker": {"enabled": false, "url": "", "token_file": "~/.config/valerie-assistant/edge_worker.token",
                    "timeout": 20}

Every request carries "Authorization: Bearer <token>"; the token is read from the token file at call time and is
never stored in the config, logged or echoed. Every function returns plain, friendly text and never raises.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import config as cfg_mod

TOPICS = ("web", "connections", "node", "cases", "all")
MAX_BYTES = 200000
MAX_TEXT = 3000


def _settings():
    c = dict(cfg_mod.load_config().get("edge_worker") or {})
    return (bool(c.get("enabled")), str(c.get("url") or "").strip().rstrip("/"),
            os.path.expanduser(str(c.get("token_file") or "")), _timeout(c))


def _timeout(c):
    try:
        return max(2, min(int(c.get("timeout", 20)), 120))
    except (TypeError, ValueError):
        return 20


def _token(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class _Unavailable(Exception):
    """Carries a friendly message."""


def _request(method, path, query=None, body=None):
    enabled, url, token_file, timeout = _settings()
    if not enabled or not url:
        raise _Unavailable("The edge worker isn't set up. Turn on \"edge_worker\" in Valerie's config "
                           "(set its url and token_file) to use it.")
    if not url.startswith(("http://", "https://")):
        raise _Unavailable("The edge worker address in the config must start with http:// or https://.")
    token = _token(token_file)
    if not token:
        raise _Unavailable("The edge worker token file is missing or empty, so I can't sign in to it.")
    full = url + path
    if query:
        full += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
               "User-Agent": "Valerie-assistant (edge worker client)"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _Unavailable("The edge worker refused my token (it may have been changed).")
        raise _Unavailable("The edge worker answered with an error (%s)." % e.code)
    except Exception as e:  # noqa: BLE001 - unreachable device, timeout, DNS ...
        raise _Unavailable("I couldn't reach the edge worker (%s). It may be off or not on this network."
                           % type(e).__name__)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise _Unavailable("The edge worker sent a reply I couldn't read.")


def _text(v):
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    s = str(v if v is not None else "").strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT] + " ...(cut)"


def _guard(fn):
    try:
        return fn()
    except _Unavailable as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "The edge worker call failed (%s)." % type(e).__name__


def status():
    def run():
        d = _request("GET", "/status")
        return "Edge worker is up. " + _text(d.get("summary") or d.get("status") or d) if isinstance(d, dict) \
            else "Edge worker is up."
    return _guard(run)


def report(topic="all", ai=False):
    topic = (topic or "all").strip().lower()
    if topic not in TOPICS:
        return "Unknown topic '%s'. Choose one of: %s." % (topic[:30], ", ".join(TOPICS))

    def run():
        q = {"topic": topic}
        if ai:
            q["ai"] = "1"
        d = _request("GET", "/report", q)
        if isinstance(d, dict):
            return _text(d.get("summary") or d.get("report") or d)
        return _text(d)
    return _guard(run)


def ask(question):
    question = (question or "").strip()
    if not question:
        return "What should I ask the edge worker?"

    def run():
        d = _request("POST", "/ask", body={"q": question[:2000]})
        if not isinstance(d, dict):
            return _text(d)
        brain = d.get("brain")
        return _text(d.get("answer", d)) + (" (answered by %s)" % _text(brain)[:60] if brain else "")
    return _guard(run)


def note(title, body, tags=None):
    title, body = (title or "").strip(), (body or "").strip()
    if not title and not body:
        return "What should the note say?"

    def run():
        _request("POST", "/note", body={"title": title[:200], "body": body[:4000], "tags": list(tags or [])})
        return "Saved the note on the edge worker."
    return _guard(run)


def skills():
    def run():
        return _text(_request("GET", "/skills"))
    return _guard(run)


def configured():
    """True when the worker is switched on and has an address (used to decide whether to advertise its tools)."""
    try:
        enabled, url, _tf, _t = _settings()
        return enabled and bool(url)
    except Exception:  # noqa: BLE001
        return False
