"""
mail_format.py - turns raw email into something readable on screen and
natural to hear read aloud.

  friendly_when()   "today at 12:15 PM", "yesterday at 9:02 AM", "Mon, Oct 5"
  sender()          display name instead of the full address
  readable_subject  "[MOC][MORNING][ASTRA][GREEN] 2026-10-07" ->
                    "MOC, Morning, Astra, Green - October 7"
  clean_body()      unwraps hard-wrapped lines, drops quoted replies,
                    signatures, links and markdown/table clutter
  key_points()      status / waiting-on / blocked / needs lines first
  spoken_list(), spoken_email()   short scripts for the voice
  speech_clean()    strips symbols, ids and links from anything spoken
"""

import datetime
import email.utils
import re

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _iso_to_words(m):
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12:
        return f"{_MONTHS[mo - 1]} {d}"
    return m.group(0)


def html_to_text(html_src):
    """Keep table rows and paragraphs as separate lines, cells as 'Key: Value'."""
    import html as _html
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_src or "")
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", ": ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def friendly_when(date_header, tzname=None):
    zone = None
    if tzname:
        from dateutil import tz as _tz
        zone = _tz.gettz(tzname)
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
        dt = dt.astimezone(zone) if zone else dt.astimezone()
    except Exception:
        return (date_header or "")[:16]
    now = datetime.datetime.now().astimezone(zone) if zone else datetime.datetime.now().astimezone()
    t = dt.strftime("%I:%M %p").lstrip("0")
    days = (now.date() - dt.date()).days
    if days == 0:
        return f"today at {t}"
    if days == 1:
        return f"yesterday at {t}"
    if 1 < days < 7:
        return f"{dt.strftime('%A')} at {t}"
    return dt.strftime("%a, %b %d").replace(" 0", " ")


def sender(from_header):
    name, addr = email.utils.parseaddr(from_header or "")
    name = name.strip().strip('"')
    return name or (addr.split("@")[0] if addr else "unknown sender")


def readable_subject(subject):
    s = subject or "(no subject)"
    tags = re.findall(r"\[([^\]]+)\]", s)
    rest = re.sub(r"\[[^\]]+\]", "", s).strip(" -:")
    rest = re.sub(r"(\d{4})-(\d{2})-(\d{2})", _iso_to_words, rest)
    if tags:
        nice = ", ".join(t.strip().title() if t.isupper() else t.strip() for t in tags)
        nice = re.sub(r"\bMoc\b", "MOC", nice)
        return f"{nice} - {rest}" if rest else nice
    return rest


_QUOTE_START = re.compile(r"^(On .+ wrote:|-----Original Message-----|From: .+|Sent from my )", re.I)


def clean_body(text):
    lines = (text or "").replace("\r", "").split("\n")
    out = []
    for ln in lines:
        if _QUOTE_START.match(ln.strip()) and out:
            break                      # rest is the quoted earlier thread
        if ln.strip() in ("--", "-- "):
            break                      # signature
        if ln.lstrip().startswith(">"):
            continue
        ln = re.sub(r"https?://\S+", "(link)", ln)
        ln = re.sub(r"[*_`#]{1,3}", "", ln)
        if re.fullmatch(r"\s*[|=\-_~ ]{3,}\s*", ln):
            continue                   # table rules / ===== lines
        ln = ln.replace("|", " - ")
        out.append(ln.rstrip())

    # "Status Ready" / "Needs Decision No" style fields -> "Status: Ready"
    _FIELDS = r"(Item ID|Department|Status|Needs Decision|Priority|Owner|Due( Date)?|Assigned To|From|To|Category|Stage|Update / Result|Result)"
    out = [re.sub(rf"^\s*{_FIELDS}\s+(?!:)(\S.*)$", lambda m: f"{m.group(1)}: {m.group(m.lastindex)}", ln)
           if len(ln) < 90 else ln for ln in out]

    # Unwrap hard-wrapped paragraphs (mail clients wrap at ~72 chars): only
    # join a line onto the previous one if the previous one was long.
    paras, cur, prev_len = [], "", 0
    for ln in out:
        wrapped = prev_len >= 60
        prev_len = len(ln)
        starts_block = (not ln.strip() or re.match(r"\s*([-•*]|\d+[.)])\s", ln)
                        or (ln.strip().isupper() and len(ln.strip()) > 3) or re.match(r"^[A-Z][\w /&()-]{1,30}:", ln))
        if not ln.strip():
            if cur:
                paras.append(cur)
                cur = ""
            continue
        is_heading = ln.strip().isupper() and len(ln.strip()) > 3
        if (starts_block or not wrapped) and cur:
            paras.append(cur)
            cur = ln.strip()
        else:
            cur = f"{cur} {ln.strip()}" if cur else ln.strip()
        if is_heading:            # a heading is its own line, never merged
            paras.append(cur)
            cur = ""
    if cur:
        paras.append(cur)
    text = "\n".join(paras)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_KEY = re.compile(r"^\s*[-•*]?\s*(ecosystem status|overall status|current health|status|blockers?|blocked|"
                  r"waiting on|needs? (decision|you|me|approval)|action required|decision needed|"
                  r"approval needed|urgent|due|needs decision)\b", re.I)
_DEPT = re.compile(r"^\s*[-•*]?\s*([A-Z][\w /&.-]{2,50}?)\s*\[(GREEN|YELLOW|RED)\]\s*$")


def key_points(body, limit=8):
    pts, seen = [], set()
    for p in body.split("\n"):
        p = p.strip()
        d = _DEPT.match(p)
        if d:
            p = f"{d.group(1).strip()}: {d.group(2).lower()}"
        elif not (_KEY.match(p) and len(p) < 350):
            continue
        key = p.lower()
        if key not in seen:
            seen.add(key)
            pts.append(p.lstrip("-•* "))
        if len(pts) >= limit:
            break
    return pts


def _say_code(m):
    words = [w for w in re.split(r"-", m.group(0)) if w and not re.fullmatch(r"\d{6,}", w) and w not in ("OUT", "IN")]
    return " ".join(w.lower() if w.isalpha() else w for w in words)


def speech_clean(text):
    t = re.sub(r"\[id [^\]]+\]\s*", "", text or "")
    # OUT-20261007-ASTRA-PACKAGE-11 -> "astra package 11"
    t = re.sub(r"\b[A-Z]{2,}(?:-[A-Z0-9]+){2,}\b", _say_code, t)
    t = re.sub(r"\[(GREEN|YELLOW|RED)\]", lambda m: m.group(1).lower(), t)
    t = t.replace("&", " and ").replace(" / ", " and ").replace("—", ",").replace("→", " to ")
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[=*#_`|>]+", " ", t)
    t = re.sub(r"(\d{4})-(\d{2})-(\d{2})", _iso_to_words, t)
    t = re.sub(r"\(\* = unread[^)]*\)", "", t)
    t = t.replace(" - ", ", ").replace("•", ",")
    return re.sub(r"\s+", " ", t).strip()


def list_line(m, tzname=None):
    star = "• NEW  " if m.get("unread") else "• "
    return f"{star}{friendly_when(m.get('date', ''), tzname)} - {sender(m.get('from'))}: {readable_subject(m.get('subject'))}   [id {m['id']}]"


def spoken_list(label, msgs, n=3, tzname=None):
    if not msgs:
        return f"{label}: no email."
    unread = [m for m in msgs if m.get("unread")]
    head = f"{label}: {len(unread)} unread." if unread else f"{label}: nothing unread."
    pick = (unread or msgs)[:n]
    items = [f"{'first' if i == 0 else 'next'}, {readable_subject(m.get('subject'))}, from {sender(m.get('from'))}, "
             f"{friendly_when(m.get('date', ''), tzname)}" for i, m in enumerate(pick)]
    return speech_clean(head + (" Latest: " if not unread else " ") + ". ".join(items) + ".")


def readable_email(m):
    body = clean_body(m.get("body", ""))
    return (f"From: {sender(m.get('from'))}\nWhen: {friendly_when(m.get('date', ''))}\n"
            f"Subject: {readable_subject(m.get('subject'))}\n\n{body}"), body


def spoken_email(m, body, max_words=110):
    intro = f"From {sender(m.get('from'))}, {friendly_when(m.get('date', ''))}. {readable_subject(m.get('subject'))}."
    pts = key_points(body)
    gist = ""
    for para in body.split("\n"):
        words_ = para.split()
        if len(words_) >= 8 and not para.strip().isupper() and not _KEY.match(para) and not para.startswith("A new entry"):
            first = re.split(r"(?<=[.!?])\s", para.strip())[0]
            gist = " ".join(first.split()[:35])
            break
    if pts:
        main = "Key points: " + ". ".join(pts) + (f". The main update: {gist}" if gist else "")
    else:
        main = "It says: " + body
    words = main.split()
    more = ""
    if len(words) > max_words:
        main = " ".join(words[:max_words])
        more = " That's the start. Say 'summarize it' for a short summary, or 'show chat' to read it all."
    return speech_clean(f"{intro} {main}.{more}")
