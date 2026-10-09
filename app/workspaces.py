"""
workspaces.py - the separate areas of someone's life or business that
Valerie keeps apart (e.g. "Work" and "Home", or two businesses).

Each workspace has its own:
  label, about (one line for the AI), tz (home time zone), color,
  keywords (words that mean "this workspace"), mail (IMAP/SMTP settings),
  board (connector name for its task board, optional),
  knowledge_dirs (folders whose documents belong to it).

config.json:
  "owner": "Sam",
  "workspaces": {"work": {...}, "home": {...}}   # insertion order = display order

Older configs (two fixed ecosystems, "mail" + "mail_coaching") are migrated
once by migrate().
"""

import os
import re

DEFAULT_COLORS = ["#b8742a", "#3f7a8a", "#6b8f5e", "#8a5a8a", "#a0522d"]

MAIL_DEFAULTS = {
    "enabled": False, "address": "", "username": "", "password": "", "from_name": "",
    "imap_host": "", "imap_port": 993, "smtp_host": "", "smtp_port": 465,
    "sent_folder": "Sent", "trash_folder": "Trash",
}


def new_workspace(label, tz="America/New_York", about="", keywords=None, color=None):
    return {"label": label, "about": about, "tz": tz, "color": color or DEFAULT_COLORS[0],
            "keywords": keywords or [label.lower()], "mail": dict(MAIL_DEFAULTS), "board": "",
            "knowledge_dirs": []}


def slug(label):
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return s[:30] or "workspace"


def migrate(cfg):
    """Older Valerie configs had two fixed mailboxes ("mail" and
    "mail_coaching"). Turn them into two generic workspaces, once.
    Returns True if anything changed."""
    if cfg.get("workspaces"):
        return False
    old = [(k, sec) for k, sec in (("author", "mail"), ("coaching", "mail_coaching")) if cfg.get(sec)]
    if not old:
        return False
    eco = cfg.get("ecosystems", {})
    cfg["workspaces"] = {}
    for i, (wid, sec) in enumerate(old):
        e = eco.get(wid, {})
        w = new_workspace(e.get("label", wid.title()), e.get("tz", "America/New_York"),
                          color=DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
        w["mail"] = dict(MAIL_DEFAULTS, **cfg.get(sec, {}))
        cfg["workspaces"][wid] = w
    return True


def all_ids(cfg):
    return list(cfg.get("workspaces", {}).keys())


def get(cfg, wid):
    return cfg.get("workspaces", {}).get(wid, {})


def label(cfg, wid):
    return get(cfg, wid).get("label") or wid.title()


def tz(cfg, wid, default="America/New_York"):
    return get(cfg, wid).get("tz") or default


def mail(cfg, wid):
    return get(cfg, wid).get("mail", {})


def board(cfg, wid):
    return get(cfg, wid).get("board", "")


def owner(cfg):
    return (cfg.get("owner") or "").strip()


def color(cfg, wid):
    ids = all_ids(cfg)
    return get(cfg, wid).get("color") or DEFAULT_COLORS[ids.index(wid) % len(DEFAULT_COLORS) if wid in ids else 0]


def _kw_pattern(words):
    words = [re.escape(w.strip().lower()) for w in words if w and w.strip()]
    return re.compile(r"\b(" + "|".join(words) + r")s?\b") if words else None


def detect(cfg, text):
    """Which workspace is this text clearly about? (None if none or several)."""
    low = (text or "").lower()
    hits = []
    for wid in all_ids(cfg):
        w = get(cfg, wid)
        pat = _kw_pattern([w.get("label", wid)] + list(w.get("keywords", [])))
        if pat and pat.search(low):
            hits.append(wid)
    return hits[0] if len(hits) == 1 else None


def detect_last(cfg, text):
    """When several workspaces are mentioned ("skip work, let's do home"),
    the one mentioned LAST wins."""
    low = (text or "").lower()
    best, pos = None, -1
    for wid in all_ids(cfg):
        w = get(cfg, wid)
        pat = _kw_pattern([w.get("label", wid)] + list(w.get("keywords", [])))
        if not pat:
            continue
        for m in pat.finditer(low):
            if m.start() > pos:
                best, pos = wid, m.start()
    return best


def classify_path(cfg, path):
    """Which workspace a document belongs to: by its folder, then by keywords
    in its path; 'shared' if neither."""
    real = os.path.realpath(path)
    for wid in all_ids(cfg):
        for d in get(cfg, wid).get("knowledge_dirs", []):
            d = os.path.realpath(os.path.expanduser(d))
            if real == d or real.startswith(d + os.sep):
                return wid
    low = path.lower()
    name = os.path.basename(low)
    for wid in all_ids(cfg):
        w = get(cfg, wid)
        for k in [w.get("label", wid)] + list(w.get("keywords", [])):
            k = k.lower().strip()
            if len(k) >= 3 and (name.startswith(k) or f"/{k}" in low):
                return wid
    return "shared"


def ai_line(cfg, wid):
    """One line telling the AI which workspace is in focus."""
    w = get(cfg, wid)
    others = [label(cfg, o) for o in all_ids(cfg) if o != wid]
    bits = [f"Workspace in focus: {label(cfg, wid).upper()}"]
    if w.get("about"):
        bits.append(f"({w['about']})")
    bits.append(f"- home time zone {tz(cfg, wid)}.")
    bits.append(f"Use mailbox account '{wid}'" + (f" and board '{w['board']}'" if w.get("board") else "") + ".")
    if others:
        bits.append(f"Do not bring up anything from {', '.join(others)}.")
    return " ".join(bits)


def persona_summary(cfg):
    ids = all_ids(cfg)
    if not ids:
        return ""
    parts = [f"'{wid}' = {label(cfg, wid)}" + (f" ({get(cfg, wid).get('about')})" if get(cfg, wid).get("about") else "")
             for wid in ids]
    return ("The owner keeps these workspaces strictly separate: " + "; ".join(parts) +
            ". Always pass the right account/board, never mix one workspace's mail, items or notes into another, "
            "and say which workspace you mean.")
