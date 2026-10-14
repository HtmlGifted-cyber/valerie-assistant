"""
settings_ui.py - Valerie's Settings window, laid out like the Claude /
ChatGPT / Gemini settings: a sidebar of sections and one page at a time,
with Save / Cancel always visible at the bottom.

Sections: General, Voice & Mic, Mail, Skills, Connectors, Tools, Schedule,
Knowledge, Calendar & Cloud.

Pages that manage files (Skills, Connectors, Schedule) act immediately and
ask before changing anything; the rest is written to config.json on Save.
"""

import json
import os
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog

import config as cfg_mod
import knowledge
import listen
import skills as skills_mod
import voices

BG = "#f6f1e7"
PANEL = "#efe6d4"
HEADER = "#2f4a3a"
GOLD = "#d9a441"
AMBER = "#b8742a"
INK = "#3b2f25"
MUTED = "#7a6a58"
FONT = "DejaVu Sans"

TOOL_LABELS = {
    "system_check": "Laptop health check", "open_calendar": "Open calendar app", "current_datetime": "Date & time",
    "search_knowledge": "Search knowledge base", "remember": "Remember facts", "kdp_cover_calc": "KDP cover calculator",
    "backup_status": "Drive backup status", "disk_usage": "Disk space", "drive_sync": "Two-way Drive sync",
    "list_files": "List folders (read-only)", "read_file": "Read files (read-only)",
    "mail_list": "Mail: list", "mail_read": "Mail: read", "mail_mark": "Mail: mark read", "mail_delete": "Mail: delete",
    "mail_send": "Mail: send (asks first)", "skill_manage": "Create/edit skills", "kb_manage": "Edit knowledge notes",
    "use_connector": "Connectors / plug-ins", "reminder": "Reminders & schedule", "decisions": "What needs my decision",
    "ask_claude": "Ask Claude (asks first)", "find_files": "Find files", "trash_files": "Move files to Trash (asks first)",
    "calendar": "Calendar events", "kdp_tracker": "Book projects tracker", "web_search": "Web search",
    "web_read": "Read web pages", "sheet_read": "Google Sheets: read", "sheet_write": "Google Sheets: write",
    "sheet_append": "Google Sheets: add row", "doc_read": "Google Docs: read", "doc_write": "Google Docs: write",
    "doc_create": "Google Docs: create",
}

CONNECTOR_TEMPLATES = {
    "php_board": {"name": "my-board", "title": "My Board", "description": "What this board is for.",
                  "type": "php_board", "url": "https://example.com/bridge.php",
                  "columns": ["Timestamp", "From", "To", "Message", "Priority", "Status", "Completed Y/N"],
                  "defaults": {"From": "Valerie", "Priority": "Normal", "Status": "Unread"},
                  "inbox_to": ["Valerie", "Me"], "confirm_writes": True, "enabled": True},
    "http": {"name": "my-service", "title": "My Service", "description": "What this service does.",
             "type": "http", "confirm_writes": True, "enabled": True,
             "actions": {"status": {"method": "GET", "url": "https://example.com/api/status?key={secret}"},
                         "send": {"method": "POST", "url": "https://example.com/api/send",
                                  "body": {"key": "{secret}", "message": "{message}"}}}},
    "sheet_board": {"name": "my-sheet-board", "title": "My Sheet Board", "description": "A Google Sheet used as a board.",
                    "type": "sheet_board", "spreadsheet_id": "PASTE_SHEET_ID", "sheet": "Sheet1",
                    "columns": ["Timestamp", "From", "To", "Message", "Priority", "Status", "Completed Y/N"],
                    "defaults": {"From": "Valerie", "Priority": "Normal", "Status": "New", "Completed Y/N": "N"},
                    "inbox_to": ["Valerie", "Me"], "confirm_writes": True, "enabled": True},
}


def _label(parent, text, size=9, bold=False, fg=INK, **kw):
    return tk.Label(parent, text=text, bg=BG, fg=fg, font=(FONT, size, "bold" if bold else "normal"),
                    justify="left", anchor="w", **kw)


def _btn(parent, text, cmd, primary=False):
    return tk.Button(parent, text=text, command=cmd, relief=tk.FLAT, cursor="hand2", padx=10, pady=3,
                     bg=AMBER if primary else PANEL, fg="white" if primary else INK, font=(FONT, 9, "bold" if primary else "normal"),
                     activebackground=GOLD)


def _section(parent, title, note=None):
    _label(parent, title, 11, True, fg=HEADER).pack(anchor="w", pady=(10, 0))
    if note:
        _label(parent, note, 8, fg=MUTED, wraplength=560).pack(anchor="w", pady=(0, 4))


class SettingsWindow:
    PAGES = ["General", "Workspaces", "Voice & Mic", "Skills", "Connectors", "Tools", "Schedule", "Knowledge",
             "Integrations", "Calendar & Cloud"]

    def __init__(self, app):
        self.app = app
        self.cfg = json.loads(json.dumps(app.config))  # edit a copy; applied on Save
        self.collectors = []
        self.win = tk.Toplevel(app.root)
        self.win.title("Valerie Settings")
        self.win.geometry("860x640")
        self.win.minsize(760, 520)
        self.win.configure(bg=BG)
        self.win.transient(app.root)

        # bottom bar
        bar = tk.Frame(self.win, bg=PANEL, pady=8)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        _btn(bar, "Save", self.save, primary=True).pack(side=tk.RIGHT, padx=(6, 16))
        _btn(bar, "Cancel", self.win.destroy).pack(side=tk.RIGHT)
        self.status = tk.Label(bar, text="", bg=PANEL, fg=MUTED, font=(FONT, 8))
        self.status.pack(side=tk.LEFT, padx=16)

        # sidebar
        side = tk.Frame(self.win, bg=HEADER, width=180)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        tk.Label(side, text="Settings", bg=HEADER, fg=GOLD, font=(FONT, 14, "bold"), pady=14).pack(anchor="w", padx=14)
        self.side_btns = {}
        for name in self.PAGES:
            b = tk.Button(side, text=name, anchor="w", relief=tk.FLAT, cursor="hand2", padx=14, pady=6,
                          bg=HEADER, fg="#e8dcc4", activebackground="#3f604c", activeforeground="white",
                          font=(FONT, 10), command=lambda n=name: self.show(n))
            b.pack(fill=tk.X)
            self.side_btns[name] = b

        # page area (scrollable)
        holder = tk.Frame(self.win, bg=BG)
        holder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.page = None
        self.win.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.win.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.win.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

        # Build every page once (so Save can collect all of them); show one.
        self.frames = {}
        for name in self.PAGES:
            f = tk.Frame(self.canvas, bg=BG, padx=24, pady=8)
            getattr(self, "page_" + name.split()[0].lower().replace("&", ""))(f)
            self.frames[name] = f
        self.show("General")

    # ------------------------------------------------------------ helpers
    def show(self, name):
        self.canvas.delete("all")
        f = self.frames[name]
        self.canvas.create_window((0, 0), window=f, anchor="nw")
        f.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(0)
        for n, b in self.side_btns.items():
            b.config(bg=GOLD if n == name else HEADER, fg=INK if n == name else "#e8dcc4",
                     font=(FONT, 10, "bold" if n == name else "normal"))

    def note(self, text):
        self.status.config(text=text)

    def bg(self, fn, done):
        self.app._in_background(fn, done)

    def ask(self, title, text):
        return messagebox.askyesno(title, text[:1500], parent=self.win)

    # ------------------------------------------------------------ General
    def page_general(self, f):
        c = self.cfg
        _section(f, "AI brain", "Ollama runs on this laptop for free. The cloud options need your own API key (Calendar & Cloud page).")
        prov = tk.StringVar(value=c.get("active_provider", "ollama"))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w")
        for val, lab in [("ollama", "Local (Ollama)"), ("omniroute", "OmniRoute (free models)"), ("anthropic", "Claude"), ("openai", "ChatGPT"), ("gemini", "Gemini")]:
            tk.Radiobutton(row, text=lab, value=val, variable=prov, bg=BG, font=(FONT, 9)).pack(side=tk.LEFT, padx=(0, 10))
        _label(f, "Local model (tool-calling: qwen2.5:3b, qwen2.5:1.5b = faster, qwen2.5:7b = smarter)", 8, fg=MUTED).pack(anchor="w", pady=(6, 0))
        model = tk.Entry(f, width=24); model.insert(0, c.get("ollama", {}).get("model", "qwen2.5:3b")); model.pack(anchor="w")

        _section(f, "How Valerie reaches you when you're away",
                 "Phone (ntfy): free push alerts and Yes/No questions on your phone - run 'venv/bin/python app/va_notify.py setup ntfy' once for your private channel. Desktop: pop-ups on this computer only. Off: no alerts.")
        ncfg = c.get("notify", {})
        notify_v = tk.StringVar(value=ncfg.get("method", "desktop"))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w")
        for val, lab in [("ntfy", "Phone (ntfy, free)"), ("desktop", "Desktop pop-ups"), ("none", "Off")]:
            tk.Radiobutton(row, text=lab, value=val, variable=notify_v, bg=BG, font=(FONT, 9)).pack(side=tk.LEFT, padx=(0, 10))

        _section(f, "Custom instructions", "Who Valerie is and how she should work - like Custom Instructions in ChatGPT/Claude.")
        persona = tk.Text(f, height=7, width=70, wrap=tk.WORD, font=(FONT, 9))
        persona.insert("1.0", c.get("assistant", {}).get("persona", ""))
        persona.pack(anchor="w")

        _section(f, "You", "Valerie greets you by this name.")
        owner_e = tk.Entry(f, width=24); owner_e.insert(0, c.get("owner", "")); owner_e.pack(anchor="w")
        import workspaces as _ws
        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=(6, 0))
        _label(row, "Focus when Valerie opens", 9, width=22).pack(side=tk.LEFT)
        focus_v = tk.StringVar(value=c.get("focus", "both"))
        for v_, t_ in [("both", "All")] + [(w, _ws.label(c, w)) for w in _ws.all_ids(c)]:
            tk.Radiobutton(row, text=t_, value=v_, variable=focus_v, bg=BG).pack(side=tk.LEFT)

        _section(f, "Behavior")
        a, t = c.get("assistant", {}), c.get("talk", {})
        speak = tk.BooleanVar(value=a.get("speak_replies", True))
        talk_start = tk.BooleanVar(value=a.get("talk_mode_on_start", True))
        wake = tk.BooleanVar(value=t.get("wake_word", True))
        for var, lab in [(speak, "Read replies aloud (same as the Voice button)"),
                         (talk_start, "Open in Talk mode (hands-free)"),
                         (wake, "Talk mode: after a minute of quiet, only answer when I say \"Valerie, ...\"")]:
            tk.Checkbutton(f, text=lab, variable=var, bg=BG, font=(FONT, 9)).pack(anchor="w")
        _label(f, "After I report on emails you asked about:", 9).pack(anchor="w", pady=(8, 0))
        after = tk.StringVar(value=c.get("mail_after_report", "trash"))
        for val, lab in [("trash", "Mark read and move to Trash (keeps anything waiting on you; 'undo' brings them back)"),
                         ("read", "Only mark them read"), ("off", "Leave them alone")]:
            tk.Radiobutton(f, text=lab, value=val, variable=after, bg=BG, font=(FONT, 9)).pack(anchor="w", padx=12)

        def collect():
            c["active_provider"] = prov.get()
            c.setdefault("notify", {})["method"] = notify_v.get()
            c.setdefault("ollama", {})["model"] = model.get().strip() or "qwen2.5:3b"
            c.setdefault("assistant", {})["persona"] = persona.get("1.0", "end").strip()
            c["assistant"]["speak_replies"] = speak.get()
            c["assistant"]["talk_mode_on_start"] = talk_start.get()
            c.setdefault("talk", {})["wake_word"] = wake.get()
            c["mail_after_report"] = after.get()
            c["owner"] = owner_e.get().strip()
            c["focus"] = focus_v.get()
        self.collectors.append(collect)

    # --------------------------------------------------------- Voice & Mic
    def page_voice(self, f):
        c = self.cfg
        vc = c.setdefault("voice", {})
        _section(f, "Voice", "Piper voices run on this laptop. Pick one, download it once, then Test.")
        labels = voices.labels()
        vvar = tk.StringVar(value=vc.get("model", "en_US-amy-medium"))
        tk.OptionMenu(f, vvar, *labels.keys()).pack(anchor="w")
        vstat = _label(f, "", 8, fg=MUTED); vstat.pack(anchor="w")

        def refresh(*_):
            v = vvar.get()
            vstat.config(text=f"{labels.get(v, v)} - {'on disk' if voices.is_downloaded(v) else 'not downloaded yet'}")
        vvar.trace_add("write", refresh); refresh()

        def download():
            v = vvar.get(); vstat.config(text=f"Downloading {v}...")
            self.bg(lambda: voices.download(v), lambda r: refresh())
        rate = tk.Scale(f, from_=0.7, to=1.4, resolution=0.05, orient="horizontal", length=320, bg=BG,
                        label="Speaking rate (lower = faster)"); rate.set(vc.get("length_scale", 1.0))
        expr = tk.Scale(f, from_=0.3, to=1.0, resolution=0.05, orient="horizontal", length=320, bg=BG,
                        label="Expressiveness (higher = less robotic)"); expr.set(vc.get("noise_scale", 0.667))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=4)
        _btn(row, "Download voice", download).pack(side=tk.LEFT)

        def test():
            vc.update(model=vvar.get(), length_scale=rate.get(), noise_scale=expr.get())
            old = self.app.config
            self.app.config = dict(old, voice=dict(vc))
            self.app.speak("This is what I sound like with these settings.", force=True)
            self.app.config = old
        _btn(row, "Test voice", test).pack(side=tk.LEFT, padx=6)
        rate.pack(anchor="w"); expr.pack(anchor="w")

        _section(f, "Microphone", "Speech-to-text runs on this laptop (faster-whisper).")
        sc = c.setdefault("speech_input", {})
        mic = tk.BooleanVar(value=sc.get("enabled", True))
        tk.Checkbutton(f, text="Enable the microphone (MIC, Hold to Talk, Talk mode)", variable=mic, bg=BG).pack(anchor="w")
        size = tk.StringVar(value=sc.get("model_size", "tiny"))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w")
        _label(row, "Recognition model: ").pack(side=tk.LEFT)
        for v, lab in [("tiny", "tiny (fastest)"), ("base", "base (more accurate)"), ("small", "small (slow)")]:
            tk.Radiobutton(row, text=lab, value=v, variable=size, bg=BG).pack(side=tk.LEFT)
        missing = listen.missing_pieces()
        if missing:
            _label(f, f"Missing: {', '.join(missing)}", 8, fg="#8a2a2a").pack(anchor="w")

        def collect():
            vc.update(model=vvar.get(), length_scale=rate.get(), noise_scale=expr.get())
            sc.update(enabled=mic.get(), model_size=size.get())
        self.collectors.append(collect)

    # ---------------------------------------------------------- Workspaces
    def page_workspaces(self, f):
        import reminders
        import workspaces as W
        c = self.cfg
        c.setdefault("workspaces", {})
        _section(f, "Workspaces", "Separate areas Valerie keeps apart - e.g. two businesses, or Work and Home. Each has "
                 "its own mailbox, board, memory, knowledge and time zone, and nothing crosses between them.")
        src = tk.StringVar(value=c.get("mail_source", "auto"))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w")
        _label(row, "Get mail from: ", 9, True).pack(side=tk.LEFT)
        for v, lab in [("auto", "Auto"), ("server", "Mail server only"), ("thunderbird", "Thunderbird only")]:
            tk.Radiobutton(row, text=lab, value=v, variable=src, bg=BG).pack(side=tk.LEFT, padx=(0, 8))
        _label(f, "Auto = live from the mail server when a password is set, otherwise Thunderbird's downloaded mail. "
               "Several wrong passwords can get you temporarily blocked by a mail server - test once.", 8, fg=MUTED,
               wraplength=560).pack(anchor="w")
        holder = tk.Frame(f, bg=BG); holder.pack(fill=tk.X, pady=(6, 0))
        widgets = {}

        def build():
            for w in holder.winfo_children():
                w.destroy()
            widgets.clear()
            boards = [""] + sorted(json.load(open(os.path.join(cfg_mod.DATA_DIR, "connectors", fn))).get("name", fn[:-5])
                                   for fn in os.listdir(os.path.join(cfg_mod.DATA_DIR, "connectors"))
                                   if fn.endswith(".json")) if os.path.isdir(os.path.join(cfg_mod.DATA_DIR, "connectors")) else [""]
            for wid in W.all_ids(c):
                wsd = c["workspaces"][wid]
                mc = wsd.setdefault("mail", dict(W.MAIL_DEFAULTS))
                box = tk.LabelFrame(holder, text=f"{wsd.get('label', wid)}  [{wid}]", bg=BG, fg=HEADER,
                                    font=(FONT, 9, "bold"), padx=8, pady=4)
                box.pack(fill=tk.X, pady=5)
                ents = {}
                gen = [("label", "Name"), ("about", "About (one line for the AI)"), ("keywords", "Keywords (comma separated)")]
                r = 0
                for k, lab in gen:
                    _label(box, lab, 8).grid(row=r, column=0, sticky="w")
                    e = tk.Entry(box, width=58)
                    v = wsd.get(k, "")
                    e.insert(0, ", ".join(v) if isinstance(v, list) else str(v))
                    e.grid(row=r, column=1, columnspan=3, sticky="w", pady=1)
                    ents[k] = e
                    r += 1
                tzv = tk.StringVar(value=wsd.get("tz", "America/New_York"))
                _label(box, "Time zone", 8).grid(row=r, column=0, sticky="w")
                tk.OptionMenu(box, tzv, *reminders.TZ_CHOICES).grid(row=r, column=1, sticky="w")
                bv = tk.StringVar(value=wsd.get("board", ""))
                _label(box, "Board connector", 8).grid(row=r, column=2, sticky="w")
                tk.OptionMenu(box, bv, *boards).grid(row=r, column=3, sticky="w")
                r += 1
                _label(box, "Knowledge folders (; separated)", 8).grid(row=r, column=0, sticky="w")
                kd = tk.Entry(box, width=58); kd.insert(0, "; ".join(wsd.get("knowledge_dirs", [])))
                kd.grid(row=r, column=1, columnspan=3, sticky="w"); r += 1
                en = tk.BooleanVar(value=mc.get("enabled", False))
                tk.Checkbutton(box, text="Use the mail server directly (read live, send, mark read, delete)", variable=en,
                               bg=BG).grid(row=r, column=0, columnspan=4, sticky="w"); r += 1
                mfields = [("address", "Email address", False), ("username", "Login (blank = address)", False),
                           ("password", "Password", True), ("from_name", "Name on sent mail", False),
                           ("imap_host", "IMAP server", False), ("imap_port", "IMAP port", False),
                           ("smtp_host", "SMTP server", False), ("smtp_port", "SMTP port", False),
                           ("sent_folder", "Sent folder", False), ("trash_folder", "Trash folder", False)]
                ments = {}
                for i, (k, lab, secret) in enumerate(mfields):
                    rr, col = divmod(i, 2)
                    _label(box, lab, 8).grid(row=r + rr, column=col * 2, sticky="w", padx=(0, 4))
                    e = tk.Entry(box, width=24, show="*" if secret else "")
                    e.insert(0, str(mc.get(k, "")))
                    e.grid(row=r + rr, column=col * 2 + 1, sticky="w", pady=1, padx=(0, 10))
                    ments[k] = e
                r += 5
                st = _label(box, "", 8, fg=MUTED, wraplength=540); st.grid(row=r + 1, column=0, columnspan=4, sticky="w")
                btns = tk.Frame(box, bg=BG); btns.grid(row=r, column=0, columnspan=4, sticky="w", pady=4)

                def test(wid=wid, st=st):
                    import mail_client
                    import thunderbird_mail
                    collect()
                    st.config(text="Testing...")

                    def run():
                        out = []
                        try:
                            out.append("Thunderbird: " + thunderbird_mail.test(wid))
                        except Exception as e:  # noqa: BLE001
                            out.append(f"Thunderbird: {e}")
                        if W.mail(c, wid).get("password"):
                            tmp = json.loads(json.dumps(c)); tmp["workspaces"][wid]["mail"]["enabled"] = True
                            out.append("Server: " + mail_client.test(tmp, wid))
                        return " | ".join(out)
                    self.bg(run, lambda r_: st.config(text=str(r_)[:240]))

                def remove(wid=wid):
                    if self.ask("Remove workspace?", f"Remove the {W.label(c, wid)} workspace from Valerie? "
                                "(Its email stays on the mail server; this only removes it here.)"):
                        collect(); c["workspaces"].pop(wid, None); build(); self.show("Workspaces")
                _btn(btns, "Test mail", test).pack(side=tk.LEFT)
                _btn(btns, "Remove workspace", remove).pack(side=tk.LEFT, padx=6)
                widgets[wid] = (ents, tzv, bv, kd, en, ments)
            add_row = tk.Frame(holder, bg=BG); add_row.pack(anchor="w", pady=8)
            _btn(add_row, "Add workspace", add, primary=True).pack(side=tk.LEFT)

        def add():
            name = simpledialog.askstring("Add workspace", "Name (e.g. Work, Home, My Bakery):", parent=self.win)
            if not name:
                return
            collect()
            wid = W.slug(name)
            while wid in c["workspaces"]:
                wid += "2"
            w = W.new_workspace(name.strip(), reminders._local_name(),
                                color=W.DEFAULT_COLORS[len(c["workspaces"]) % len(W.DEFAULT_COLORS)])
            c["workspaces"][wid] = w
            build(); self.show("Workspaces")

        def collect():
            c["mail_source"] = src.get()
            for wid, (ents, tzv, bv, kd, en, ments) in widgets.items():
                if wid not in c["workspaces"]:
                    continue
                w = c["workspaces"][wid]
                w["label"] = ents["label"].get().strip() or wid
                w["about"] = ents["about"].get().strip()
                w["keywords"] = [k.strip().lower() for k in ents["keywords"].get().split(",") if k.strip()]
                w["tz"] = tzv.get()
                w["board"] = bv.get()
                w["knowledge_dirs"] = [d.strip() for d in kd.get().split(";") if d.strip()]
                mc = w.setdefault("mail", {})
                for k, e in ments.items():
                    v = e.get().strip()
                    mc[k] = int(v) if k.endswith("_port") and v.isdigit() else v
                mc["enabled"] = en.get()
        build()
        self.collectors.append(collect)

    # -------------------------------------------------------- Integrations
    def page_integrations(self, f):
        c = self.cfg
        ig = c.setdefault("integrations", {})
        _section(f, "Optional integrations", "Extra tools that only appear once they're set up. Leave blank to keep them off.")
        rows = [("drive_sync_script", "Sync script", "A script taking status / preview / sync (e.g. an rclone bisync wrapper)."),
                ("backup_log", "Backup log", "A log file to report backup status from (e.g. an rclone log)."),
                ("rclone_backup_remote", "Back up Valerie to", "An rclone remote, e.g. gdrive: - backs up skills, notes and settings (no passwords) nightly."),
                ("book_series_dir", "Book projects folder", "A folder with one sub-folder per book - enables the /books tracker and KDP calculator.")]
        ents = {}
        for k, lab, hint in rows:
            _label(f, lab, 9, True).pack(anchor="w", pady=(8, 0))
            _label(f, hint, 8, fg=MUTED, wraplength=560).pack(anchor="w")
            e = tk.Entry(f, width=70); e.insert(0, ig.get(k, "")); e.pack(anchor="w")
            ents[k] = e

        def collect():
            for k, e in ents.items():
                ig[k] = e.get().strip()
            tools = c.setdefault("enabled_tools", {})
            tools["drive_sync"] = bool(ig.get("drive_sync_script"))
            tools["backup_status"] = bool(ig.get("backup_log"))
            tools["kdp_tracker"] = tools["kdp_cover_calc"] = bool(ig.get("book_series_dir"))
        self.collectors.append(collect)

    # -------------------------------------------------------------- Skills
    def page_skills(self, f):
        _section(f, "Skills", "Reusable instructions Valerie runs as /name - like custom GPTs or Claude skills. "
                 "Valerie can also create and edit these herself when you ask.")
        wrap = tk.Frame(f, bg=BG); wrap.pack(fill=tk.X)
        lb = tk.Listbox(wrap, height=12, width=34, font=(FONT, 9), activestyle="none", selectbackground=GOLD)
        lb.pack(side=tk.LEFT, fill=tk.Y)
        detail = tk.Text(wrap, height=12, width=46, wrap=tk.WORD, font=(FONT, 8), bg="white", state="disabled")
        detail.pack(side=tk.LEFT, padx=(8, 0), fill=tk.BOTH, expand=True)
        state = {"skills": {}}

        def reload(select=None):
            state["skills"] = skills_mod.load_skills()
            lb.delete(0, tk.END)
            for n in state["skills"]:
                lb.insert(tk.END, f"/{n}")
            names = list(state["skills"])
            if select in names:
                lb.selection_set(names.index(select)); show_detail()

        def current():
            sel = lb.curselection()
            return list(state["skills"])[sel[0]] if sel else None

        def show_detail(*_):
            n = current()
            detail.config(state="normal"); detail.delete("1.0", tk.END)
            if n:
                sk = state["skills"][n]
                detail.insert("1.0", f"/{n}\n{sk['description']}\n\n{sk['prompt']}")
            detail.config(state="disabled")
        lb.bind("<<ListboxSelect>>", show_detail)

        def editor(title, name="", desc="", prompt=""):
            d = tk.Toplevel(self.win); d.title(title); d.configure(bg=BG, padx=14, pady=10); d.transient(self.win)
            _label(d, "Name (used as /name)").pack(anchor="w")
            en = tk.Entry(d, width=40); en.insert(0, name); en.pack(anchor="w")
            _label(d, "What it does (one line)").pack(anchor="w", pady=(6, 0))
            ed = tk.Entry(d, width=70); ed.insert(0, desc); ed.pack(anchor="w")
            _label(d, "Instructions - put {input} where your details go").pack(anchor="w", pady=(6, 0))
            ep = tk.Text(d, height=12, width=70, wrap=tk.WORD, font=(FONT, 9)); ep.insert("1.0", prompt); ep.pack(anchor="w")
            res = {}

            def ok():
                res.update(name=en.get().strip(), desc=ed.get().strip(), prompt=ep.get("1.0", "end").strip()); d.destroy()
            row = tk.Frame(d, bg=BG); row.pack(anchor="e", pady=8)
            _btn(row, "Cancel", d.destroy).pack(side=tk.LEFT, padx=4)
            _btn(row, "Save skill", ok, primary=True).pack(side=tk.LEFT)
            d.grab_set(); self.win.wait_window(d)
            return res

        def save_skill(r, select):
            import self_edit
            try:
                msg = self_edit.skill_manage("save", r["name"], r["desc"], r["prompt"], confirm=self.ask)
                self.note(msg); reload(self_edit._slug(r["name"])); self.app._refresh_status()
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Skill", str(e), parent=self.win)

        def new():
            r = editor("New skill")
            if r.get("name") and r.get("prompt"):
                save_skill(r, r["name"])

        def edit():
            n = current()
            if not n:
                return
            sk = state["skills"][n]
            r = editor(f"Edit /{n}", n, sk["description"], sk["prompt"])
            if r.get("name") and r.get("prompt"):
                save_skill(r, r["name"])

        def duplicate():
            n = current()
            if n:
                sk = state["skills"][n]
                r = editor("Duplicate skill", n + "-copy", sk["description"], sk["prompt"])
                if r.get("name") and r.get("prompt"):
                    save_skill(r, r["name"])

        def delete():
            import self_edit
            n = current()
            if n:
                try:
                    self.note(self_edit.skill_manage("delete", n, confirm=self.ask)); reload(); self.app._refresh_status()
                except Exception as e:  # noqa: BLE001
                    self.note(str(e))

        def run():
            n = current()
            if not n:
                return
            details = simpledialog.askstring(f"Run /{n}", "Details for this skill:", parent=self.win)
            if details is not None:
                self.win.destroy()
                self.app.handle_command(f"/{n} {details}")

        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=8)
        for lab, fn in [("New skill", new), ("Edit", edit), ("Duplicate", duplicate), ("Delete", delete), ("Run now", run)]:
            _btn(row, lab, fn, primary=(lab == "New skill")).pack(side=tk.LEFT, padx=(0, 6))
        _btn(row, "Open folder", lambda: subprocess.Popen(["xdg-open", cfg_mod.SKILLS_DIR])).pack(side=tk.LEFT)
        reload()

    # ---------------------------------------------------------- Connectors
    def page_connectors(self, f):
        import connectors as conn_mod
        c = self.cfg
        _section(f, "Connectors & plug-ins", "Outside services Valerie can use - boards, bridges and web APIs. "
                 "Each is a small JSON file; keys are stored separately in your private settings.")
        wrap = tk.Frame(f, bg=BG); wrap.pack(fill=tk.X)
        lb = tk.Listbox(wrap, height=8, width=38, font=(FONT, 9), activestyle="none", selectbackground=GOLD)
        lb.pack(side=tk.LEFT, fill=tk.Y)
        detail = tk.Text(wrap, height=8, width=44, wrap=tk.WORD, font=(FONT, 8), bg="white", state="disabled")
        detail.pack(side=tk.LEFT, padx=(8, 0), fill=tk.BOTH, expand=True)
        out = tk.Text(f, height=6, width=84, wrap=tk.WORD, font=(FONT, 8), bg="white", state="disabled")
        state = {"items": []}
        cdir = conn_mod.CONNECTORS_DIR
        os.makedirs(cdir, exist_ok=True)

        def files():
            return sorted(x for x in os.listdir(cdir) if x.endswith(".json"))

        def load(fn):
            with open(os.path.join(cdir, fn)) as fh:
                return json.load(fh)

        def reload(select=None):
            lb.delete(0, tk.END); state["items"] = []
            for fn in files():
                try:
                    d = load(fn)
                except Exception:
                    d = {"name": fn[:-5], "type": "?", "enabled": False}
                name = d.get("name") or fn[:-5]
                has_key = bool(c.get("connector_secrets", {}).get(name))
                on = d.get("enabled", True)
                lb.insert(tk.END, f"{'●' if on else '○'} {d.get('title', name)}{'  (key set)' if has_key else ''}")
                state["items"].append((fn, d))
                if select == name:
                    lb.selection_set(tk.END)
            show()

        def current():
            sel = lb.curselection()
            return state["items"][sel[0]] if sel else (None, None)

        def setbox(box, text):
            box.config(state="normal"); box.delete("1.0", tk.END); box.insert("1.0", text); box.config(state="disabled")

        def show(*_):
            fn, d = current()
            if not d:
                setbox(detail, "Select a connector."); return
            setbox(detail, f"{d.get('title', d.get('name'))}  [{d.get('type')}]  {'enabled' if d.get('enabled', True) else 'disabled'}\n"
                           f"{d.get('description', '')}\n\n{d.get('url') or d.get('spreadsheet_id') or ''}\n"
                           f"Writes ask first: {'yes' if d.get('confirm_writes', True) else 'NO'}")
        lb.bind("<<ListboxSelect>>", show)

        def json_editor(title, data):
            dlg = tk.Toplevel(self.win); dlg.title(title); dlg.configure(bg=BG, padx=12, pady=10); dlg.transient(self.win)
            _label(dlg, "Connector definition (JSON). Put API keys in 'Set API key', not here - use {secret} where the key goes.", 8, fg=MUTED).pack(anchor="w")
            t = tk.Text(dlg, height=24, width=86, font=("DejaVu Sans Mono", 9)); t.insert("1.0", json.dumps(data, indent=2)); t.pack()
            res = {}

            def ok():
                try:
                    res["data"] = json.loads(t.get("1.0", "end"))
                    if not res["data"].get("name") or res["data"].get("type") not in ("php_board", "http", "sheet_board"):
                        raise ValueError("needs a 'name' and a 'type' of php_board, http or sheet_board")
                    dlg.destroy()
                except ValueError as e:
                    messagebox.showerror("Connector", f"Not valid: {e}", parent=dlg)
            row = tk.Frame(dlg, bg=BG); row.pack(anchor="e", pady=6)
            _btn(row, "Cancel", dlg.destroy).pack(side=tk.LEFT, padx=4)
            _btn(row, "Save connector", ok, primary=True).pack(side=tk.LEFT)
            dlg.grab_set(); self.win.wait_window(dlg)
            return res.get("data")

        def write(data, old_fn=None):
            import self_edit
            fn = f"{self_edit._slug(data['name'])}.json"
            path = os.path.join(cdir, fn)
            if old_fn and os.path.exists(os.path.join(cdir, old_fn)):
                self_edit._keep_version(os.path.join(cdir, old_fn))
                if old_fn != fn:
                    os.remove(os.path.join(cdir, old_fn))
            with open(path, "w") as fh:
                json.dump(data, fh, indent=2)
            reload(data["name"]); self.note(f"Saved connector {data['name']}.")

        def add():
            kind = simpledialog.askstring("Add connector", "Type: php_board (PHP bridge), http (web API) or sheet_board (Google Sheet)",
                                          initialvalue="php_board", parent=self.win)
            if kind in CONNECTOR_TEMPLATES:
                data = json_editor("New connector", CONNECTOR_TEMPLATES[kind])
                if data:
                    write(data)

        def edit():
            fn, d = current()
            if d:
                data = json_editor(f"Edit {d.get('name')}", d)
                if data:
                    write(data, old_fn=fn)

        def toggle():
            fn, d = current()
            if d:
                d["enabled"] = not d.get("enabled", True)
                with open(os.path.join(cdir, fn), "w") as fh:
                    json.dump(d, fh, indent=2)
                reload(d.get("name"))

        def set_key():
            fn, d = current()
            if not d:
                return
            name = d.get("name") or fn[:-5]
            k = simpledialog.askstring("API key", f"API key for {d.get('title', name)} (stored in your private settings):",
                                       show="*", parent=self.win)
            if k is not None:
                c.setdefault("connector_secrets", {})[name] = k.strip()
                reload(name); self.note("Key set - click Save to keep it.")

        def test():
            fn, d = current()
            if not d:
                return
            name = d.get("name") or fn[:-5]
            action = "inbox" if d.get("type") in ("php_board", "sheet_board") else next(iter(d.get("actions", {"status": 0})))
            setbox(out, f"Testing {name} ({action})...")
            tmp = dict(self.app.config, connector_secrets=c.get("connector_secrets", {}))
            self.bg(lambda: conn_mod.run(name, action, {"limit": 3}, cfg=tmp), lambda r: setbox(out, str(r)[:1500]))

        def delete():
            import self_edit
            fn, d = current()
            if d and self.ask("Delete connector?", f"Remove {d.get('title', d.get('name'))}? A copy is kept in .versions."):
                self_edit._keep_version(os.path.join(cdir, fn)); os.remove(os.path.join(cdir, fn)); reload()

        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=8)
        for lab, fn_ in [("Add connector", add), ("Edit", edit), ("Enable / disable", toggle), ("Set API key", set_key),
                         ("Test", test), ("Delete", delete)]:
            _btn(row, lab, fn_, primary=(lab == "Add connector")).pack(side=tk.LEFT, padx=(0, 6))
        _label(f, "Test result", 8, fg=MUTED).pack(anchor="w")
        out.pack(anchor="w", fill=tk.X)
        reload()

    # --------------------------------------------------------------- Tools
    def page_tools(self, f):
        c = self.cfg
        _section(f, "Tools", "Switch individual abilities on or off. Fewer tools = faster answers from the local AI. "
                 "Google Sheets/Docs tools need the web app connector (Calendar & Cloud page).")
        en = c.setdefault("enabled_tools", {})
        grid = tk.Frame(f, bg=BG); grid.pack(anchor="w")
        vars_ = {}
        for i, (k, lab) in enumerate(TOOL_LABELS.items()):
            v = tk.BooleanVar(value=en.get(k, True))
            tk.Checkbutton(grid, text=lab, variable=v, bg=BG, font=(FONT, 9)).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 30))
            vars_[k] = v

        def collect():
            for k, v in vars_.items():
                en[k] = v.get()
        self.collectors.append(collect)

    # ------------------------------------------------------------ Schedule
    def page_schedule(self, f):
        import reminders
        _section(f, "Scheduled tasks & reminders", "Each task runs in its own time zone (a workspace task uses that "
                 "workspace's zone). Tasks run while Valerie is open. Unattended check-ins never mark or delete email.")
        cols = tk.Frame(f, bg=BG); cols.pack(anchor="w")
        lb = tk.Listbox(cols, height=12, width=92, font=("DejaVu Sans Mono", 8), activestyle="none", selectbackground=GOLD)
        lb.pack(side=tk.LEFT)
        state = {"items": []}

        def reload():
            lb.delete(0, tk.END)
            state["items"] = sorted(reminders._load(), key=lambda i: i["next"])
            for i in state["items"]:
                import workspaces as W
                eco = (W.label(self.cfg, i.get("ecosystem"))[:7].upper() if i.get("ecosystem") in W.all_ids(self.cfg)
                       else {"both": "ALL"}.get(i.get("ecosystem"), "-"))
                on = " " if i.get("enabled", True) else "(paused) "
                lb.insert(tk.END, f"{on}{eco:7}{i['text'][:34]:35}{reminders.describe(i)}")

        def current():
            sel = lb.curselection()
            return state["items"][sel[0]] if sel else None

        def editor(item):
            d = tk.Toplevel(self.win); d.title("Scheduled task"); d.configure(bg=BG, padx=14, pady=10); d.transient(self.win)
            g = tk.Frame(d, bg=BG); g.pack(anchor="w")
            row = 0

            def lab(t):
                nonlocal row
                _label(g, t, 9).grid(row=row, column=0, sticky="nw", pady=3, padx=(0, 8))

            lab("Name"); name = tk.Entry(g, width=46); name.insert(0, item.get("text", "")); name.grid(row=row, column=1, sticky="w"); row += 1
            lab("Ecosystem")
            eco = tk.StringVar(value=item.get("ecosystem", "none"))
            er = tk.Frame(g, bg=BG); er.grid(row=row, column=1, sticky="w")
            tzvar = tk.StringVar(value=item.get("tz") or reminders._local_name())

            def eco_changed(*_):
                if eco.get() in W.all_ids(self.cfg):
                    tzvar.set(reminders.eco_tz(eco.get(), self.cfg))
            import workspaces as W
            for v, t in [(w, W.label(self.cfg, w)) for w in W.all_ids(self.cfg)] + [("both", "All"), ("none", "Personal")]:
                tk.Radiobutton(er, text=t, value=v, variable=eco, bg=BG, command=eco_changed).pack(side=tk.LEFT)
            row += 1
            lab("Action")
            act = tk.StringVar(value=item.get("action", "speak"))
            tk.OptionMenu(g, act, *reminders.ACTIONS.keys()).grid(row=row, column=1, sticky="w")
            acthelp = _label(g, "", 8, fg=MUTED); acthelp.grid(row=row + 1, column=1, sticky="w")
            act.trace_add("write", lambda *_: acthelp.config(text=reminders.ACTIONS.get(act.get(), "")))
            acthelp.config(text=reminders.ACTIONS.get(act.get(), "")); row += 2
            lab("Skill (for 'skill')")
            skv = tk.StringVar(value=item.get("skill", ""))
            names = list(skills_mod.load_skills()) or [""]
            tk.OptionMenu(g, skv, *names).grid(row=row, column=1, sticky="w"); row += 1
            lab("Instructions / prompt")
            pr = tk.Text(g, height=6, width=56, wrap=tk.WORD, font=(FONT, 9)); pr.insert("1.0", item.get("prompt", ""))
            pr.grid(row=row, column=1, sticky="w"); row += 1
            _label(g, "For 'Ask the AI': what Valerie should do. For 'Run a skill': the details. For 'Spoken reminder': what to say.",
                   8, fg=MUTED, wraplength=420).grid(row=row, column=1, sticky="w"); row += 1
            lab("Run time (HH:MM, 24h)"); tm = tk.Entry(g, width=8); tm.insert(0, item.get("time", "09:00")); tm.grid(row=row, column=1, sticky="w"); row += 1
            lab("Time zone")
            tk.OptionMenu(g, tzvar, *reminders.TZ_CHOICES).grid(row=row, column=1, sticky="w"); row += 1
            lab("Repeat")
            rep = tk.StringVar(value=item.get("repeat", "daily"))
            rr = tk.Frame(g, bg=BG); rr.grid(row=row, column=1, sticky="w")
            for v in reminders.REPEATS:
                tk.Radiobutton(rr, text="once" if v == "none" else v, value=v, variable=rep, bg=BG).pack(side=tk.LEFT)
            row += 1
            lab("Weekday (weekly)")
            wd = tk.StringVar(value=reminders.WEEKDAYS[int(item.get("weekday", 0))].title())
            tk.OptionMenu(g, wd, *[w.title() for w in reminders.WEEKDAYS]).grid(row=row, column=1, sticky="w"); row += 1
            lab("Date (once, YYYY-MM-DD)"); dt = tk.Entry(g, width=12); dt.insert(0, item.get("date") or ""); dt.grid(row=row, column=1, sticky="w"); row += 1
            en = tk.BooleanVar(value=item.get("enabled", True))
            tk.Checkbutton(g, text="Enabled", variable=en, bg=BG).grid(row=row, column=1, sticky="w"); row += 1
            preview = _label(d, "", 8, fg=HEADER); preview.pack(anchor="w", pady=(6, 0))

            def gather():
                return dict(item, text=name.get().strip() or "Task", ecosystem=eco.get(), action=act.get(),
                            skill=skv.get(), prompt=pr.get("1.0", "end").strip(), time=tm.get().strip(),
                            tz=tzvar.get(), repeat=rep.get(), weekday=[w.title() for w in reminders.WEEKDAYS].index(wd.get()),
                            date=dt.get().strip() or None, enabled=en.get())

            def show_next(*_):
                try:
                    it = gather()
                    it["next"] = reminders.compute_next(it).isoformat(timespec="minutes")
                    preview.config(text="Next run: " + reminders.describe(it))
                except Exception as e:  # noqa: BLE001
                    preview.config(text=f"({e})")
            for w in (tm, dt):
                w.bind("<KeyRelease>", show_next)
            for v in (rep, tzvar, wd, eco):
                v.trace_add("write", show_next)
            show_next()

            def ok():
                try:
                    reminders.upsert(gather())
                    d.destroy(); reload(); self.note("Task saved.")
                except Exception as e:  # noqa: BLE001
                    messagebox.showerror("Task", str(e), parent=d)
            row_b = tk.Frame(d, bg=BG); row_b.pack(anchor="e", pady=8)
            _btn(row_b, "Cancel", d.destroy).pack(side=tk.LEFT, padx=4)
            _btn(row_b, "Save task", ok, primary=True).pack(side=tk.LEFT)
            d.grab_set()

        def new():
            focus = getattr(self.app, "focus", "both")
            import workspaces as W
            ids = W.all_ids(self.cfg)
            eco = focus if focus in ids else (ids[0] if ids else "none")
            editor({"text": "", "action": "prompt", "ecosystem": eco, "time": "09:00",
                    "tz": reminders.eco_tz(eco, self.cfg), "repeat": "weekdays", "enabled": True})

        def edit():
            it = current()
            if it:
                editor(it)

        def toggle():
            it = current()
            if it:
                reminders.upsert(dict(it, enabled=not it.get("enabled", True))); reload()

        def run_now():
            it = current()
            if it:
                self.win.destroy()
                self.app._run_scheduled(dict(it))

        def delete():
            it = current()
            if it and self.ask("Remove task?", f"Remove '{it['text']}' ({reminders.describe(it)})?"):
                reminders.remove(it["id"]); reload()

        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=8)
        for lab_, fn in [("New task", new), ("Edit", edit), ("Pause / resume", toggle), ("Run now", run_now), ("Delete", delete)]:
            _btn(row, lab_, fn, primary=(lab_ == "New task")).pack(side=tk.LEFT, padx=(0, 6))
        lb.bind("<Double-Button-1>", lambda e: edit())
        reload()

    # ----------------------------------------------------------- Knowledge
    def page_knowledge(self, f):
        c = self.cfg
        _section(f, "Knowledge base", "Documents Valerie searches when she answers. Her own notes live in the first folder.")
        try:
            st = knowledge.stats()
            info = f"{st['files']} files, {st['passages']} passages indexed."
        except Exception as e:  # noqa: BLE001
            info = f"Index unavailable: {e}"
        stat = _label(f, info); stat.pack(anchor="w")
        _label(f, "Folders (one per line):", 8, fg=MUTED).pack(anchor="w", pady=(6, 0))
        dirs = tk.Text(f, height=7, width=84, font=(FONT, 8))
        dirs.insert("1.0", "\n".join(c.get("knowledge", {}).get("dirs", [])))
        dirs.pack(anchor="w")

        def reindex():
            c.setdefault("knowledge", {})["dirs"] = [l.strip() for l in dirs.get("1.0", "end").splitlines() if l.strip()]
            stat.config(text="Indexing...")
            self.bg(lambda: knowledge.build_index(c),
                    lambda r: stat.config(text=f"{r['files']} files, {r['passages']} passages indexed." if isinstance(r, dict) else str(r)))
        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=8)
        _btn(row, "Re-index now", reindex, primary=True).pack(side=tk.LEFT)
        _btn(row, "Open my notes folder", lambda: subprocess.Popen(["xdg-open", cfg_mod.KNOWLEDGE_DIR])).pack(side=tk.LEFT, padx=6)

        def collect():
            c.setdefault("knowledge", {})["dirs"] = [l.strip() for l in dirs.get("1.0", "end").splitlines() if l.strip()]
        self.collectors.append(collect)

    # ---------------------------------------------------- Calendar & Cloud
    def page_calendar(self, f):
        c = self.cfg
        _section(f, "Calendar", "Google Calendar > Settings > your calendar > 'Secret address in iCal format'. One link per line.")
        cal = tk.Text(f, height=3, width=84, font=(FONT, 8))
        cal.insert("1.0", "\n".join(c.get("calendar", {}).get("ics_urls", [])))
        cal.pack(anchor="w")
        cstat = _label(f, "", 8, fg=MUTED); cstat.pack(anchor="w")

        def test_cal():
            import va_extras
            tmp = dict(c, calendar={"ics_urls": [l.strip() for l in cal.get("1.0", "end").splitlines() if l.strip()]})
            cstat.config(text="Loading...")
            self.bg(lambda: va_extras.calendar_events(tmp, 7), lambda r: cstat.config(text=str(r)[:300]))
        _btn(f, "Test calendar", test_cal).pack(anchor="w", pady=4)

        _section(f, "Cloud AI (optional)", "Paste a key only if you want Claude, ChatGPT or Gemini as the brain "
                 "(pick it on the General page). These are pay-per-use API keys, separate from chat subscriptions.")
        keys = {}
        for sec, lab in [("anthropic", "Claude (Anthropic) API key"), ("openai", "ChatGPT (OpenAI) API key"),
                         ("gemini", "Gemini (Google) API key")]:
            row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=1)
            _label(row, lab, 9, width=26).pack(side=tk.LEFT)
            e = tk.Entry(row, width=44, show="*"); e.insert(0, c.get(sec, {}).get("api_key", "")); e.pack(side=tk.LEFT)
            keys[sec] = e

        _section(f, "Google web app connector (Sheets & Docs)", "Your Apps Script web app. The token must match SHARED_TOKEN in the script.")
        row = tk.Frame(f, bg=BG); row.pack(anchor="w")
        _label(row, "Web app URL", 9, width=26).pack(side=tk.LEFT)
        url = tk.Entry(row, width=44); url.insert(0, c.get("web_app", {}).get("url", "")); url.pack(side=tk.LEFT)
        row = tk.Frame(f, bg=BG); row.pack(anchor="w", pady=1)
        _label(row, "Shared token", 9, width=26).pack(side=tk.LEFT)
        tok = tk.Entry(row, width=44, show="*"); tok.insert(0, c.get("web_app", {}).get("token", "")); tok.pack(side=tk.LEFT)
        wstat = _label(f, "", 8, fg=MUTED); wstat.pack(anchor="w")

        def test_wa():
            from connector import WebAppConnector
            wstat.config(text="Testing...")
            w = WebAppConnector(url.get().strip(), tok.get().strip())
            self.bg(lambda: f"Connected: {w.health_check()}", lambda r: wstat.config(text=str(r)[:200]))
        _btn(f, "Test web app", test_wa).pack(anchor="w", pady=4)

        def collect():
            c.setdefault("calendar", {})["ics_urls"] = [l.strip() for l in cal.get("1.0", "end").splitlines() if l.strip()]
            for sec, e in keys.items():
                c.setdefault(sec, {})["api_key"] = e.get().strip()
            c.setdefault("web_app", {}).update(url=url.get().strip(), token=tok.get().strip())
        self.collectors.append(collect)

    # ---------------------------------------------------------------- Save
    def save(self):
        try:
            for collect in self.collectors:
                collect()
            cfg_mod.save_config(self.cfg)
            self.app.config = self.cfg
            self.app._refresh_status()
            self.app._paint_voice_toggle()
            messagebox.showinfo("Valerie", "Settings saved.", parent=self.win)
            self.win.destroy()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            messagebox.showerror("Valerie", f"Settings were NOT saved:\n{e}", parent=self.win)
