"""
va_extras.py - the VA add-ons (Oct 2026):

  decisions_digest()   items waiting on the owner, one section per workspace
  ask_claude()         hand a hard task to Claude Code (read-only, approved)
  free_job()           start a background free-job on a personal project
  free_job_result()    get the recent log output of free jobs
  find_files()         search both drives by name
  trash_files()        move files to the desktop Trash (recoverable, approved)
  calendar_events()    today's / upcoming events from private iCal links
  kdp_tracker()        status of each book project in the configured folder
  backup_valerie()     copy Valerie's skills/notes/connectors to Google Drive
  web_search(), web_read()
"""

import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

import requests

import config as cfg_mod

HOME = os.path.expanduser("~")
def search_roots(cfg=None):
    """Home (or configured roots) plus every mounted removable drive."""
    cfg = cfg or cfg_mod.load_config()
    roots = [os.path.expanduser(r) for r in cfg.get("files", {}).get("search_roots", [HOME])]
    media = os.path.join("/media", os.environ.get("USER", ""))
    if os.path.isdir(media):
        roots += [os.path.join(media, d) for d in sorted(os.listdir(media)) if os.path.ismount(os.path.join(media, d))]
    return [r for r in dict.fromkeys(roots) if os.path.isdir(r)]


class ExtraError(Exception):
    pass


ACTIVE_PROCS = []   # long-running subprocesses the STOP button may kill


def kill_active():
    for p in list(ACTIVE_PROCS):
        try:
            p.kill()
        except Exception:
            pass
    ACTIVE_PROCS.clear()


def _run_proc(cmd, timeout, cwd=None):
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ACTIVE_PROCS.append(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise
    finally:
        if p in ACTIVE_PROCS:
            ACTIVE_PROCS.remove(p)
    if p.returncode and p.returncode < 0:
        raise ExtraError("Stopped.")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _approve(confirm, title, text):
    if confirm is None:
        raise ExtraError("This needs the owner's approval in the Valerie window.")
    if not confirm(title, text):
        raise ExtraError("Declined - nothing was done.")


# ------------------------------------------------------------- decisions

_DECISION_SRC = (
    r"(needs? decision\s*[:\-]?\s*(yes|y\b)|approval[_ ]required|awaiting (your |{OWNER}'?s? )?(approval|decision|ruling)|"
    r"pending (your |{OWNER}'?s? )?(approval|decision|ruling)|waiting on ({OWNER}|you)\b|"
    r"needs? ({OWNER}|your) (approval|decision|ruling|input|sign-?off)|action required|decision needed|approval needed|"
    r"for ({OWNER}'?s|your) (approval|review|decision)|approval backlog|awaiting {OWNER})")


def _decision_re(owner=None):
    owner = re.escape((owner or cfg_mod.load_config().get("owner") or "the owner").strip().lower()) or "owner"
    return re.compile(_DECISION_SRC.replace("{OWNER}", owner), re.I)


_DECISION = _decision_re("owner")
# "Waiting On: ..." / "Blockers: ..." lines count unless they say none/nothing.
_OPEN_FIELD = re.compile(r"^\s*[-•*]?\s*(waiting on|blockers?|blocked|needs decision|decisions? needed)\s*:\s*(.+)$", re.I)
_NONE = re.compile(r"^(none|nothing|n/?a|no\b|-|—)", re.I)


def _decision_lines(body, owner=None):
    dre = _decision_re(owner) if owner else _DECISION
    out = []
    for ln in body.split("\n"):
        m = _OPEN_FIELD.match(ln)
        if m:
            val = m.group(2).strip()
            if not _NONE.match(val) and not re.match(r"none (on|stopping|from)", val, re.I):
                out.append(f"{m.group(1).title()}: {val}")
            continue
        if dre.search(ln):
            out.append(ln.strip(" -•*"))
    return out


def decisions_digest(cfg, per_account=12, ecosystems=None):
    import mail_format
    import mail_router
    import connectors as conn
    out = []
    total = 0
    import workspaces
    owner = workspaces.owner(cfg) or "owner"
    dre = _decision_re(owner)
    for acct in workspaces.all_ids(cfg):
        if ecosystems and acct not in ecosystems:
            continue
        eco, board = workspaces.label(cfg, acct).upper(), workspaces.board(cfg, acct)
        found = []
        try:
            _src, msgs, _ = mail_router.list_messages(cfg, acct, "INBOX", limit=per_account)
            cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=3)
            for m in msgs:
                try:
                    import email.utils
                    if email.utils.parsedate_to_datetime(m["date"]) < cutoff and not m["unread"]:
                        continue
                except Exception:
                    pass
                full = mail_router.read_message(cfg, acct, m["id"])
                body = mail_format.clean_body(full.get("body", ""))
                lines = _decision_lines(body, owner)
                if lines or dre.search(m.get("subject", "")):
                    snippet = "; ".join(l[:220] for l in lines[:4]) or mail_format.readable_subject(m["subject"])
                    found.append(f"Email from {mail_format.sender(m['from'])} ({mail_format.friendly_when(m['date'])}), "
                                 f"{mail_format.readable_subject(m['subject'])}: {snippet}   [id {m['id']}]")
        except Exception as e:  # noqa: BLE001
            found.append(f"(couldn't check the {acct} inbox: {str(e)[:120]})")
        if board:
            try:
                r = conn.run(board, "inbox", {"To": workspaces.owner(cfg) or "", "limit": 10}, cfg=cfg)
                items = [ln for ln in r.split("\n") if ln.startswith("[row")]
                found += [f"Board: {ln[:220]}" for ln in items]
            except Exception:
                pass  # some boards can't be read; mail is the main source
        total += sum(1 for f in found if not f.startswith("("))
        out.append(f"=== {eco} - waiting on you ===\n" + ("\n".join(f"- {f}" for f in found) if found else "- Nothing found."))
    return total, "\n\n".join(out)


# ------------------------------------------------------------ ask claude

def ask_claude(question, confirm=None, timeout=420):
    claude = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")
    if not os.path.exists(claude):
        raise ExtraError("Claude Code isn't installed on this laptop.")
    _approve(confirm, "Ask Claude?", "This sends the question below to Claude (uses your Claude plan):\n\n" + question[:1200])
    work = os.path.join(cfg_mod.DATA_DIR, "claude-scratch")
    os.makedirs(work, exist_ok=True)
    cmd = [claude, "-p", question, "--output-format", "text", "--permission-mode", "plan",
           "--append-system-prompt",
           "You are answering for Valerie, a local desktop voice assistant. Answer directly and concisely in plain "
           "text that reads well aloud (no tables). Do not modify any files."]
    try:
        p = _run_proc(cmd, timeout, cwd=work)
    except subprocess.TimeoutExpired:
        raise ExtraError(f"Claude didn't answer within {timeout // 60} minutes.")
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        raise ExtraError(f"Claude Code returned an error: {(p.stderr or out)[:300]}")
    return out


# ------------------------------------------------------------ free job

_PENDING_JOB = os.path.join(cfg_mod.DATA_DIR, "pending-free-job.json")
_PENDING_MINUTES = 10


def _free_job_check(project, task):
    """Shared safety checks. Returns the cleaned project name; raises ExtraError otherwise."""
    projects_dir = os.path.join(HOME, "Projects")
    if not os.path.isdir(projects_dir):
        raise ExtraError("Projects directory does not exist.")
    valid = sorted(d for d in os.listdir(projects_dir)
                   if not d.startswith(".") and os.path.isdir(os.path.join(projects_dir, d)))
    if not project or "/" in project or "\\" in project or project in (".", "..") or project not in valid:
        raise ExtraError(f"Invalid or unknown project '{project}'. Available projects: {', '.join(valid)}")
    if not task or not isinstance(task, str):
        raise ExtraError("Task must be a non-empty string.")
    if len(task) > 2000:
        raise ExtraError("Task description is too long (max 2000 characters).")
    p = subprocess.run(["pgrep", "-f", "claude-free"], capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        raise ExtraError("A free job is already running.")
    if not (shutil.which("free-job") or os.path.exists(os.path.join(HOME, ".local", "bin", "free-job"))):
        raise ExtraError("free-job script is not installed.")
    return project


def _free_job_start(project, task):
    script = shutil.which("free-job") or os.path.join(HOME, ".local", "bin", "free-job")
    subprocess.Popen([script, project, task], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"Started free job on {project}. Ask me 'free job result' in a few minutes."


def free_job(project, task, confirm=None):
    """Desktop window: asks with a dialog. Phone/web chat (no dialog): queues the job and returns a one-time code
    that only the owner's own typed 'approve <code>' message can redeem - the chat layer handles that message
    directly, so the AI model can never approve its own request."""
    project = _free_job_check(project, task)
    if confirm is not None:
        _approve(confirm, "Start Free Job?", f"This will start a free background job on project '{project}' with task:\n\n{task[:1200]}")
        return _free_job_start(project, task)
    return _queue_pending({"kind": "free_job", "project": project, "task": task},
                          f"Free job on '{project}':\n{task[:600]}")


def _queue_pending(payload, summary):
    """Park ONE action awaiting the owner's typed 'approve <code>' (shared by free_job and MCP calls)."""
    import secrets
    code = "%06d" % secrets.randbelow(1000000)
    with open(_PENDING_JOB, "w") as f:
        json.dump(dict(payload, code=code, created=time.time(), tries=0), f)
    try:
        os.chmod(_PENDING_JOB, 0o600)
    except OSError:
        pass
    return (f"Waiting for your OK. {summary}\n\n"
            f"To start it, type exactly: approve {code}   (valid {_PENDING_MINUTES} minutes). "
            f"To drop it, type: cancel free job.")


def owner_gate(kind, title, summary, payload, confirm=None):
    """Owner approval for a non-free-job action, same mechanism as free_job: a dialog when there is a window,
    otherwise a one-time code only the owner's own typed message can redeem (never the AI model)."""
    if confirm is not None:
        _approve(confirm, title, summary)
        return _run_approved(kind, payload)
    return _queue_pending(dict(payload, kind=kind), summary)


def _run_approved(kind, payload):
    if kind == "mcp":
        import va_mcp
        return va_mcp.execute_approved(payload)
    return "Unknown kind of waiting action."


def free_job_approve(code):
    """Called by the chat layer when the OWNER types 'approve <code>'. Never exposed as an AI tool."""
    try:
        with open(_PENDING_JOB) as f:
            p = json.load(f)
    except (OSError, ValueError):
        return "There is nothing waiting for your approval."
    if time.time() - p.get("created", 0) > _PENDING_MINUTES * 60:
        os.remove(_PENDING_JOB)
        return "That free job request expired. Ask me again."
    if str(code) != p.get("code"):
        p["tries"] = p.get("tries", 0) + 1
        if p["tries"] >= 3:
            os.remove(_PENDING_JOB)
            return "Wrong code three times - the request was dropped."
        with open(_PENDING_JOB, "w") as f:
            json.dump(p, f)
        return "That code doesn't match the waiting free job."
    os.remove(_PENDING_JOB)
    if p.get("kind", "free_job") != "free_job":
        cfg_mod.log_activity("owner_approved", f"kind={p.get('kind')}")
        return _run_approved(p["kind"], p)
    try:
        project = _free_job_check(p["project"], p["task"])
    except ExtraError as e:
        return str(e)
    cfg_mod.log_activity("free_job", f"approved project={project!r} task={p['task'][:200]!r}")
    return _free_job_start(project, p["task"])


def free_job_cancel():
    try:
        os.remove(_PENDING_JOB)
        return "Dropped the waiting free job."
    except OSError:
        return "There was no free job waiting."


def free_job_result():
    config_dir = os.path.join(HOME, ".config", "free-jobs")
    if not os.path.isdir(config_dir):
        return "No free jobs yet."

    logs = glob.glob(os.path.join(config_dir, "*.log"))
    if not logs:
        return "No free jobs yet."

    latest_log = max(logs, key=os.path.getmtime)
    mtime = os.path.getmtime(latest_log)
    age_seconds = datetime.datetime.now().timestamp() - mtime
    status_str = "finished/likely still running" if age_seconds <= 90 else "finished"

    try:
        with open(latest_log, "r", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return "No free jobs yet."

    filtered_lines = []
    for l in lines:
        if re.search(r'^\[claude-code:|^claude-free:|unrecognized|doesn.t recognize the model|connectors are disabled', l):
            continue
        filtered_lines.append(l)

    last_25 = "".join(filtered_lines[-25:])

    log_name = os.path.basename(latest_log)
    return f"Log: {log_name} (status: {status_str}, modified {int(age_seconds)}s ago):\n\n{last_25}"


# ----------------------------------------------------------------- files

_FILE_INDEX = os.path.join(cfg_mod.DATA_DIR, "file_index.json")
_SKIP = ("/.cache/", "/.git/", "/node_modules/", "/venv/", "/.local/lib/", "/.npm/", "/snap/", "/.mozilla/",
         "/.config/google-chrome/", "/.Trash", "/.rclone-cache", "/.thunderbird/", "/.claude/")


def build_file_index():
    entries = []
    for root in search_roots():
        for dp, dirs, files in os.walk(root):
            if any(s in dp + "/" for s in _SKIP):
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not (d.startswith(".") and root == HOME and dp == HOME and d not in (".local",))]
            for f in files:
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append([p, st.st_size, int(st.st_mtime)])
    with open(_FILE_INDEX, "w") as fh:
        json.dump({"built": datetime.datetime.now().isoformat(timespec="minutes"), "files": entries}, fh)
    return len(entries)


def find_files(query, limit=15):
    if not os.path.exists(_FILE_INDEX) or (datetime.datetime.now().timestamp() - os.path.getmtime(_FILE_INDEX)) > 6 * 3600:
        build_file_index()
    with open(_FILE_INDEX) as fh:
        idx = json.load(fh)
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1 and w not in ("the", "my", "file", "files", "find", "where", "is")]
    if not words:
        raise ExtraError("Tell me part of the file or folder name.")
    hits = []
    for p, size, mtime in idx["files"]:
        low = p.lower()
        name = os.path.basename(low)
        if all(w in low for w in words):
            score = sum(3 for w in words if w in name) + (2 if all(w in name for w in words) else 0)
            hits.append((score, mtime, p, size))
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    if not hits:
        return f"No files matching '{query}' (index from {idx['built']})."
    lines = [f"{os.path.basename(p)}  ({size / 1048576:.1f} MB, {datetime.date.fromtimestamp(m)})\n    {os.path.dirname(p)}"
             for _s, m, p, size in hits[:limit]]
    more = f"\n...and {len(hits) - limit} more." if len(hits) > limit else ""
    return f"{len(hits)} match(es) for '{query}':\n" + "\n".join(lines) + more


def trash_files(paths, confirm=None):
    paths = [os.path.realpath(os.path.expanduser(p)) for p in paths if p]
    allowed = list(dict.fromkeys([HOME] + search_roots()))
    bad = [p for p in paths if not any(p.startswith(a + os.sep) for a in allowed)]
    if bad:
        raise ExtraError(f"Only files in your home folder or the external drives can be trashed: {bad[0]}")
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise ExtraError(f"Not found: {missing[0]}")
    if any(p.startswith(os.path.join(HOME, ".")) for p in paths):
        raise ExtraError("I won't trash hidden/system files in your home folder.")
    size = sum(os.path.getsize(p) for p in paths if os.path.isfile(p))
    _approve(confirm, f"Move {len(paths)} item(s) to the Trash?",
             f"{size / 1048576:.0f} MB, recoverable from the Trash:\n\n" + "\n".join(paths[:25]) +
             (f"\n...and {len(paths) - 25} more" if len(paths) > 25 else ""))
    gio = shutil.which("gio")
    done = 0
    for p in paths:
        r = subprocess.run([gio, "trash", p], capture_output=True, text=True) if gio else None
        if r is not None and r.returncode == 0:
            done += 1
    return f"Moved {done} of {len(paths)} item(s) to the Trash ({size / 1048576:.0f} MB). They can be restored from the Trash."


def trash_duplicates_of(folder, keep_folder, confirm=None):
    """Trash files in `folder` that are exact duplicates of files in `keep_folder`
    (same relative name and size), e.g. duplicate copies of videos kept in two folders."""
    folder, keep_folder = os.path.realpath(folder), os.path.realpath(keep_folder)
    dupes = []
    for dp, _d, files in os.walk(folder):
        for f in files:
            p = os.path.join(dp, f)
            k = os.path.join(keep_folder, os.path.relpath(p, folder))
            if os.path.isfile(k) and os.path.getsize(k) == os.path.getsize(p):
                dupes.append(p)
    if not dupes:
        return "No duplicates found between those folders."
    return trash_files(dupes, confirm)


# -------------------------------------------------------------- calendar

def _ics_dt(val):
    val = val.split(":")[-1].strip()
    try:
        if len(val) == 8:
            return datetime.datetime.strptime(val, "%Y%m%d"), True
        if val.endswith("Z"):
            return datetime.datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None), False
        return datetime.datetime.strptime(val[:15], "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, False


def calendar_events(cfg, days=1):
    urls = [u for u in cfg.get("calendar", {}).get("ics_urls", []) if u]
    if not urls:
        raise ExtraError("No calendar connected yet. In Google Calendar: Settings > your calendar > "
                         "'Secret address in iCal format' - paste it in Valerie's Settings > Calendar.")
    start = datetime.datetime.combine(datetime.date.today(), datetime.time())
    end = start + datetime.timedelta(days=days)
    events = []
    for url in urls:
        try:
            text = requests.get(url, timeout=20).text
        except requests.RequestException as e:
            raise ExtraError(f"Couldn't load the calendar: {e}")
        text = re.sub(r"\r?\n[ \t]", "", text)  # unfold lines
        for block in text.split("BEGIN:VEVENT")[1:]:
            fields = {}
            for line in block.split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fields.setdefault(k.split(";")[0].strip(), (line, v.strip()))
            if "DTSTART" not in fields:
                continue
            dt, all_day = _ics_dt(fields["DTSTART"][0])
            if not dt:
                continue
            summary = fields.get("SUMMARY", ("", "(no title)"))[1].replace("\\,", ",")
            rrule = fields.get("RRULE", ("", ""))[1]
            occurrences = [dt]
            if rrule:  # simple daily/weekly repeats only
                freq = re.search(r"FREQ=(\w+)", rrule)
                step = {"DAILY": 1, "WEEKLY": 7}.get(freq.group(1) if freq else "", 0)
                until = re.search(r"UNTIL=(\w+)", rrule)
                until_dt = _ics_dt(until.group(1))[0] if until else None
                if step:
                    occurrences = []
                    d = dt
                    while d < end and (until_dt is None or d <= until_dt):
                        if d >= start:
                            occurrences.append(d)
                        d += datetime.timedelta(days=step)
            for occ in occurrences:
                if start <= occ < end:
                    events.append((occ, all_day, summary))
    events.sort()
    if not events:
        return "Nothing on your calendar " + ("today." if days == 1 else f"in the next {days} days.")
    lines = [f"{'All day' if ad else d.strftime('%I:%M %p').lstrip('0')}{'' if days == 1 else ' ' + d.strftime('%a')}: {s}"
             for d, ad, s in events]
    return ("Today: " if days == 1 else f"Next {days} days: ") + "; ".join(lines)


# ------------------------------------------------------------------- KDP

def _pdf_pages(path):
    m = re.search(r"_(\d{2,3})p\.pdf$", path)
    if m:
        return int(m.group(1))
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return len(re.findall(rb"/Type\s*/Page[^s]", data)) or None
    except OSError:
        return None


def _docx_words(path):
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        return len(re.sub(r"<[^>]+>", " ", xml).split())
    except Exception:
        return None


def kdp_tracker(title_filter=None):
    series = os.path.expanduser(cfg_mod.load_config().get("integrations", {}).get("book_series_dir", "") or "")
    if not series:
        raise ExtraError("No book projects folder is set (Settings > Integrations).")
    if not os.path.isdir(series):
        raise ExtraError(f"The book projects folder isn't available: {series} (is the drive plugged in?).")
    rows = []
    book_dirs = [d for d in sorted(glob.glob(os.path.join(series, "*")))
                 if os.path.isdir(d) and not os.path.basename(d)[:2].isdigit()]
    names = [os.path.basename(d) for d in book_dirs]
    common = os.path.commonprefix(names) if len(names) > 1 else ""
    common = common[:common.rfind(" ") + 1] if " " in common else ""
    for d in book_dirs:
        title = os.path.basename(d)[len(common):] or os.path.basename(d)
        if title_filter and title_filter.lower() not in title.lower():
            continue
        files = os.listdir(d)
        low = [f.lower() for f in files]
        interior = [os.path.join(d, f) for f in files if "interior" in f.lower() and f.lower().endswith(".pdf")]
        pages = max((p for p in (_pdf_pages(i) for i in interior) if p), default=None)
        docx = [os.path.join(d, f) for f in files if f.lower().endswith(".docx")]
        words = max((w for w in (_docx_words(x) for x in docx) if w), default=None)
        est = pages or (round(words / 250) if words else None)  # ~250 words per 6x9 page
        wrap = any("wrap" in f for f in low)
        cover = any(f.endswith((".jpg", ".png")) and "cover" in f for f in low)
        epub = any(f.endswith(".epub") for f in low)
        if pages and pages >= 24 and wrap and cover:
            stage = "upload files complete (24+ page interior, wrap, cover)"
        elif est and est >= 24:
            stage = "long enough - needs " + ", ".join(x for x, ok in [("interior PDF", bool(interior)), ("wrap", wrap), ("cover", cover)] if not ok)
        elif est:
            stage = f"needs expansion (~{est} pages, {max(0, 24 - est)}+ to go)"
        else:
            stage = "no manuscript found"
        rows.append((title, est, stage, epub))
    if not rows:
        return "No matching titles."
    ready = [r for r in rows if r[2].startswith("upload files complete")]
    expand = [r for r in rows if r[2].startswith("needs expansion")]
    near = [r for r in rows if r[2].startswith("long enough")]
    head = (f"{os.path.basename(series)}: {len(rows)} titles. {len(ready)} have complete upload files, {len(near)} long enough but missing "
            f"files, {len(expand)} need expansion to 24+ pages.")
    if title_filter:
        return head + "\n" + "\n".join(f"- {t}: {s}{' (has eBook)' if e else ''}" for t, _p, s, e in rows)
    show = ready[:10] + near[:10] + sorted(expand, key=lambda r: -(r[1] or 0))[:10]
    return head + "\n\nClosest to income first:\n" + "\n".join(f"- {t}: {s}" for t, _p, s, _e in show)


# --------------------------------------------------------- Valerie backup

def backup_valerie(cfg):
    """Copy skills, knowledge notes and connectors + settings WITHOUT
    passwords/keys to Google Drive (author account) 'Valerie Backup'."""
    stage = os.path.join(cfg_mod.DATA_DIR, "backup-stage")
    shutil.rmtree(stage, ignore_errors=True)
    for sub in ("knowledge", "skills", "connectors"):
        src = os.path.join(cfg_mod.DATA_DIR, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(stage, sub), ignore=shutil.ignore_patterns(".versions"))
    clean = json.loads(json.dumps(cfg))
    for w in clean.get("workspaces", {}).values():
        w.get("mail", {}).pop("password", None)
    for sec in ("mail", "mail_coaching"):
        clean.get(sec, {}).pop("password", None)
    for sec in ("anthropic", "openai", "gemini"):
        clean.get(sec, {}).pop("api_key", None)
    clean.get("web_app", {}).pop("token", None)
    clean.pop("connector_secrets", None)
    clean.get("calendar", {}).pop("ics_urls", None)  # secret links
    with open(os.path.join(stage, "config-no-secrets.json"), "w") as fh:
        json.dump(clean, fh, indent=2)
    remote = cfg.get("integrations", {}).get("rclone_backup_remote", "")
    if not remote:
        raise ExtraError("No backup destination is set (Settings > Integrations, e.g. 'gdrive:').")
    if not remote.endswith(":") and not remote.endswith("/"):
        remote += "/"
    p = subprocess.run(["rclone", "copy", stage, remote + "Valerie Backup", "--update"],
                       capture_output=True, text=True, timeout=600)
    shutil.rmtree(stage, ignore_errors=True)
    if p.returncode != 0:
        errs = [l for l in (p.stderr or "").splitlines() if "client_id" not in l]
        raise ExtraError("Backup to Google Drive failed: " + (errs[-1][:200] if errs else "unknown error"))
    return f"Valerie's skills, notes, connectors and settings (no passwords) are backed up to {remote}Valerie Backup."


# ----------------------------------------------------------------- web

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Valerie/2.2"}


def web_search(query, n=5):
    import html as _h
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=_UA, timeout=20)
    results = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    out = []
    for href, title, snip in results[:n]:
        m = re.search(r"uddg=([^&]+)", href)
        url = requests.utils.unquote(m.group(1)) if m else href
        clean = lambda s: _h.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        out.append(f"{clean(title)}\n  {clean(snip)[:220]}\n  {url}")
    return "\n".join(out) or "No results."


def web_read(url, max_chars=5000):
    import mail_format
    r = requests.get(url, headers=_UA, timeout=25)
    text = mail_format.html_to_text(r.text) if "html" in r.headers.get("content-type", "") else r.text
    return text[:max_chars]


# ---------------------------------------------------------- daily report

def daily_report(cfg):
    """Newest Ecosystem-Report_<YYYY-MM-DD>.txt in cfg["daily_report"]["dir"].
    Returns (date_str or None, is_today, text). A report from an earlier day
    is flagged stale - don't treat it as today's news."""
    import glob
    d = os.path.expanduser(cfg.get("daily_report", {}).get("dir", ""))
    files = sorted(glob.glob(os.path.join(d, "Ecosystem-Report_*.txt"))) if d else []
    if not files:
        return None, False, "No ecosystem report has been downloaded yet."
    path = files[-1]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
    date = m.group(1) if m else None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read().lstrip("﻿")
    return date, date == datetime.date.today().isoformat(), text


def daily_report_text(cfg):
    date, fresh, text = daily_report(cfg)
    if not date:
        return text
    if not fresh:
        return (f"STALE: the newest ecosystem report is from {date}, not today. Today's hasn't come in yet "
                f"(it is pulled from Google Drive every hour). Don't report it as today's news.\n\n{text[:3000]}")
    return f"Today's ecosystem report ({date}):\n\n{text[:6000]}"
