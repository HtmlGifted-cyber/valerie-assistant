"""
reminders.py - Valerie's schedule (runs while Valerie is open).

Each task:
  id, text (name), enabled
  action:    speak | checkin | report | decisions | prompt | skill | backup
  ecosystem: <workspace id> | both | none
  prompt:    instructions (AI prompt, skill details, or what to say)
  skill:     skill name (action "skill")
  time:      "HH:MM" in the task's time zone
  tz:        the task's own time zone (defaults to its workspace's)
  repeat:    none | daily | weekdays | weekends | weekly
  weekday:   0-6 (Mon-Sun) for weekly; date "YYYY-MM-DD" for one-off
  next:      next run, laptop-local time (ISO) - computed

parse_when() understands spoken forms ("in 20 minutes", "at 3pm",
"tomorrow at 9", "every weekday at 8:30am", "monday at 10am").
"""

import datetime
import json
import os
import re
import uuid

from dateutil import tz as dtz

import config as cfg_mod

PATH = os.path.join(cfg_mod.DATA_DIR, "reminders.json")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ACTIONS = {
    "checkin": "Check-in (email, board, systems)",
    "report": "Ecosystem report (email + waiting on you)",
    "decisions": "What needs my decision",
    "prompt": "Ask the AI (my instructions)",
    "skill": "Run a skill",
    "speak": "Spoken reminder",
    "backup": "Back up Valerie to Google Drive",
}
REPEATS = ["none", "daily", "weekdays", "weekends", "weekly"]
TZ_CHOICES = ["America/New_York", "America/Detroit", "America/Chicago", "America/Denver", "America/Phoenix",
              "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Vancouver",
              "Europe/London", "Europe/Paris", "Europe/Berlin", "Australia/Sydney", "Asia/Tokyo", "UTC"]


def _local_name():
    try:
        with open("/etc/timezone") as f:
            return f.read().strip() or "UTC"
    except OSError:
        return "UTC"


def _ws_ids(cfg=None):
    import workspaces
    return workspaces.all_ids(cfg or cfg_mod.load_config())


def eco_tz(eco, cfg=None):
    import workspaces
    cfg = cfg or cfg_mod.load_config()
    return workspaces.tz(cfg, eco, _local_name()) if eco in workspaces.all_ids(cfg) else _local_name()


def default_schedule(cfg=None):
    """Morning / midday / evening check-ins for each workspace."""
    import workspaces
    cfg = cfg or cfg_mod.load_config()
    out = []
    for wid in workspaces.all_ids(cfg):
        lab = workspaces.label(cfg, wid)
        for when, t in (("morning", "09:00"), ("midday", "12:30"), ("evening", "20:00")):
            out.append({"text": f"{lab} {when} check-in", "action": "checkin", "ecosystem": wid,
                        "time": t, "repeat": "daily", "tz": workspaces.tz(cfg, wid, _local_name())})
    if cfg.get("integrations", {}).get("rclone_backup_remote"):
        out.append({"text": "Back up Valerie", "action": "backup", "ecosystem": "none", "time": "21:00",
                    "repeat": "daily", "tz": _local_name()})
    return out


class ReminderError(Exception):
    pass


# --------------------------------------------------------------- storage

def _normalize(i):
    """Fill fields for tasks saved by older versions."""
    i.setdefault("id", uuid.uuid4().hex[:6])
    i.setdefault("enabled", True)
    i.setdefault("action", "speak")
    i.setdefault("prompt", "")
    eco = i.setdefault("ecosystem", "none")
    i.setdefault("tz", eco_tz(eco))
    if "time" not in i:
        try:
            nxt = datetime.datetime.fromisoformat(i["next"])
            i["time"] = nxt.strftime("%H:%M")
            i.setdefault("date", nxt.date().isoformat())
        except (KeyError, ValueError):
            i["time"] = "09:00"
    i.setdefault("repeat", "none")
    if i["repeat"] == "weekly" and "weekday" not in i:
        try:
            i["weekday"] = datetime.datetime.fromisoformat(i["next"]).weekday()
        except (KeyError, ValueError):
            i["weekday"] = 0
    if "next" not in i:
        i["next"] = compute_next(i).isoformat(timespec="minutes")
    return i


def _load():
    if not os.path.exists(PATH):
        items = [_normalize(dict(d)) for d in default_schedule()]
        _save(items)
        return items
    try:
        with open(PATH) as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    return [_normalize(i) for i in items]


def _save(items):
    with open(PATH, "w") as f:
        json.dump(items, f, indent=2)


def migrate_to_segmented():
    """One-time: older schedules had combined check-ins; replace them with
    per-workspace ones."""
    items = _load()
    ids = _ws_ids()
    if not ids or any(i.get("ecosystem") in ids and i["action"] == "checkin" for i in items):
        return False
    items = [i for i in items if i["action"] not in ("checkin", "backup")]
    items += [_normalize(dict(d)) for d in default_schedule()]
    _save(items)
    return True


# ------------------------------------------------------------ next run

def _local_tz():
    return dtz.tzlocal()


def compute_next(item, after=None):
    """Next run as a naive LOCAL datetime, honoring the task's own time zone."""
    zone = dtz.gettz(item.get("tz") or _local_name()) or _local_tz()
    after = (after or datetime.datetime.now()).replace(tzinfo=_local_tz()).astimezone(zone)
    h, m = (int(x) for x in item.get("time", "09:00").split(":"))
    rep = item.get("repeat", "none")
    if rep == "none":
        day = datetime.date.fromisoformat(item["date"]) if item.get("date") else after.date()
        cand = datetime.datetime.combine(day, datetime.time(h, m), tzinfo=zone)
        if not item.get("date") and cand <= after:
            cand += datetime.timedelta(days=1)
    else:
        cand = datetime.datetime.combine(after.date(), datetime.time(h, m), tzinfo=zone)
        for _ in range(15):
            ok = cand > after and (
                rep == "daily" or (rep == "weekdays" and cand.weekday() < 5) or
                (rep == "weekends" and cand.weekday() >= 5) or
                (rep == "weekly" and cand.weekday() == int(item.get("weekday", 0))))
            if ok:
                break
            cand = datetime.datetime.combine(cand.date() + datetime.timedelta(days=1), datetime.time(h, m), tzinfo=zone)
    return cand.astimezone(_local_tz()).replace(tzinfo=None)


# ------------------------------------------------------------- parsing

def _parse_clock(s):
    s = s.strip().lower().replace(".", "")
    if s in ("noon", "midday"):
        return 12, 0
    if s == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", s)
    if not m:
        raise ReminderError(f"I didn't understand the time '{s}'.")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap and ap.startswith("p") and h < 12:
        h += 12
    if ap and ap.startswith("a") and h == 12:
        h = 0
    if not ap and h < 7:
        h += 12
    return h, mi


def parse_when(text):
    """-> (datetime local, repeat). Raises ReminderError if no time found."""
    s = text.lower()
    now = datetime.datetime.now()
    repeat = "none"
    if re.search(r"\bevery (week)?day\b|\bdaily\b|\beach day\b", s):
        repeat = "daily"
    if re.search(r"\bevery weekday\b|\bweekdays\b", s):
        repeat = "weekdays"
    if re.search(r"\bweekends?\b", s):
        repeat = "weekends"
    m = re.search(r"\bin (\d+|an?|one|two|three|half an?) ?(minutes?|mins?|hours?|hrs?)\b", s)
    if m:
        if m.group(1).startswith("half"):
            return now + datetime.timedelta(minutes=30), repeat
        n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        delta = datetime.timedelta(hours=n) if m.group(2).startswith("h") else datetime.timedelta(minutes=n)
        return now + delta, repeat
    clock = re.search(r"\b(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)|\d{1,2}:\d{2}|noon|midnight)\b", s) \
        or re.search(r"\bat (\d{1,2})\b", s)
    if not clock:
        if "tonight" in s:
            clock_s = "8pm"
        elif "this afternoon" in s:
            clock_s = "3pm"
        elif "morning" in s:
            clock_s = "9am"
        else:
            raise ReminderError("When? e.g. 'at 3pm', 'in 20 minutes', 'tomorrow at 9'.")
    else:
        clock_s = clock.group(1)
    if "tonight" in s and not re.search(r"(am|pm)", clock_s):
        clock_s += "pm"
    h, mi = _parse_clock(clock_s)
    day = now.date()
    if "tomorrow" in s:
        day += datetime.timedelta(days=1)
    for i, wd in enumerate(WEEKDAYS):
        if re.search(rf"\b(on |next |this |every )?{wd}s?\b", s):
            day = now.date() + datetime.timedelta(days=(i - now.weekday()) % 7 or 7)
            if repeat == "none" and re.search(rf"\bevery {wd}\b|\b{wd}s\b", s):
                repeat = "weekly"
    t = datetime.datetime.combine(day, datetime.time(h, mi))
    if t <= now:
        t += datetime.timedelta(days=1)
    if repeat == "weekdays":
        while t.weekday() >= 5:
            t += datetime.timedelta(days=1)
    return t, repeat


# ----------------------------------------------------------------- API

def add(text, when_text=None, when=None, action="speak", repeat=None, ecosystem="none", prompt="", tz=None):
    if when is None:
        when, rep = parse_when(when_text or text)
        repeat = repeat or rep
    item = {"id": uuid.uuid4().hex[:6], "text": text.strip() or "Reminder", "action": action,
            "ecosystem": ecosystem, "prompt": prompt, "enabled": True,
            "tz": tz or eco_tz(ecosystem),
            "repeat": repeat or "none"}
    # store the wall-clock time in the task's own zone
    zone = dtz.gettz(item["tz"])
    local = when.replace(tzinfo=_local_tz()).astimezone(zone)
    item["time"] = local.strftime("%H:%M")
    item["date"] = local.date().isoformat()
    if item["repeat"] == "weekly":
        item["weekday"] = local.weekday()
    item["next"] = when.replace(second=0, microsecond=0).isoformat(timespec="minutes")
    items = _load()
    items.append(item)
    _save(items)
    return item


def upsert(item):
    """Create or replace a task from the editor; recomputes next run."""
    item = dict(item)
    item.setdefault("id", uuid.uuid4().hex[:6])
    if not re.fullmatch(r"\d{1,2}:\d{2}", item.get("time", "")):
        raise ReminderError("Time must look like 09:00 or 14:30.")
    if item.get("repeat") == "none" and not item.get("date"):
        raise ReminderError("A one-time task needs a date (YYYY-MM-DD).")
    if dtz.gettz(item.get("tz", "")) is None:
        raise ReminderError(f"Unknown time zone '{item.get('tz')}'.")
    item["next"] = compute_next(item).isoformat(timespec="minutes")
    items = [i for i in _load() if i["id"] != item["id"]] + [_normalize(item)]
    _save(items)
    return item


def remove(match):
    items = _load()
    keep = [i for i in items if not (i["id"] == match or match.lower() in i["text"].lower())]
    if len(keep) == len(items):
        raise ReminderError(f"No reminder matching '{match}'.")
    _save(keep)
    return len(items) - len(keep)


def listing():
    items = sorted(_load(), key=lambda i: i["next"])
    if not items:
        return "No reminders or scheduled tasks."
    return "Schedule:\n" + "\n".join(
        f"- {describe(i)}: {i['text']}{'' if i.get('enabled', True) else ' (paused)'}   [{i['id']}]" for i in items)


def pop_due(now=None):
    """Return enabled tasks that are due and roll repeats forward (drop
    one-offs). Anything more than 2 hours overdue (laptop off) is skipped."""
    now = now or datetime.datetime.now()
    items = _load()
    due, keep = [], []
    changed = False
    for i in items:
        t = datetime.datetime.fromisoformat(i["next"])
        if t <= now:
            changed = True
            if i.get("enabled", True) and now - t <= datetime.timedelta(hours=2):
                due.append(dict(i))
            if i["repeat"] == "none":
                continue
            i["next"] = compute_next(i, after=now).isoformat(timespec="minutes")
        keep.append(i)
    if changed:
        _save(keep)
    return due


def describe(item):
    t = datetime.datetime.fromisoformat(item["next"])
    today = datetime.date.today()
    day = "today" if t.date() == today else ("tomorrow" if t.date() == today + datetime.timedelta(days=1)
                                              else t.strftime("%a %b %d").replace(" 0", " "))
    here = t.strftime("%I:%M %p").lstrip("0")
    zone = item.get("tz", "")
    zone_short = zone.split("/")[-1].replace("_", " ")
    own = item.get("time", "")
    try:
        h, m = (int(x) for x in own.split(":"))
        own_s = datetime.time(h, m).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        own_s = own
    tl = t.replace(tzinfo=_local_tz())
    zone_obj = dtz.gettz(zone) if zone else None
    same = zone_obj is None or tl.utcoffset() == tl.astimezone(zone_obj).utcoffset()
    when = f"{day} {own_s} {zone_short}" + ("" if same else f" ({here} here)")
    rep = "" if item["repeat"] == "none" else f", {item['repeat']}"
    return when + rep
