"""
gui_bot.py - Valerie Local Assistant desktop GUI (2.2, user-space build).

Fixes over 2.1.7:
  - Routing: the old keyword router matched substrings, so any message
    containing "yes" (yesterday, eyes) opened the calendar and anything
    containing "help"/"status" got a canned reply instead of reaching
    the AI. Now only exact short commands are intercepted.
  - The AI call ran on the Tk main thread, freezing the window for the
    whole (often minutes-long) local inference. It now runs in a worker
    thread and posts back with root.after.
  - Conversation memory: recent exchanges are sent with each message.
  - speak() built a shell command from model output (echo "...") - a
    reply containing $(...) or backticks would have been executed. Text
    is now piped to Piper over stdin with no shell involved.

New:
  - Knowledge base (knowledge.py) - local docs injected into answers.
  - Skills (skills.py) - "/skill-name details" prompt templates.
  - Commands: /help, /skills, /kb, /reindex, /clear, /remember <fact>.
"""

import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

try:
    import psutil
except ImportError:
    psutil = None

import config as cfg_mod
import knowledge
import listen
import mail_format
import workspaces as ws
import skills as skills_mod
import voices
from brain import run_agent_loop

# Piper lives in the user's own folder on new installs (downloaded by the
# first-run wizard); older installs kept it under /usr/share.
PIPER_DIR = (cfg_mod.USER_PIPER_DIR if os.path.exists(os.path.join(cfg_mod.USER_PIPER_DIR, "piper"))
             else os.path.join(cfg_mod.SYSTEM_DIR, "piper"))
BUNDLED_PIPER = os.path.join(PIPER_DIR, "piper")
VENV_PIPER = os.path.join(cfg_mod.SYSTEM_DIR, "venv", "bin", "piper")

# Warm earth-tone palette (deep green, amber, gold, warm neutrals).
BG = "#f6f1e7"          # warm parchment
PANEL = "#efe6d4"       # light sand
HEADER = "#2f4a3a"      # deep forest green
GOLD = "#d9a441"        # amber gold
AMBER = "#b8742a"       # burnt amber (buttons)
INK = "#3b2f25"         # dark warm brown text
MUTED = "#7a6a58"
YOU_BUBBLE = "#e4ecdf"  # soft sage
BOT_BUBBLE = "#fbf3e2"  # cream
FONT = "DejaVu Sans"

HELP_TEXT = (
    "Just talk to me normally - I'll use your knowledge base and tools as needed.\n"
    "Commands:\n"
    "  /skills - list skills (e.g. /proofread <text>)\n"
    "  /remember <fact> - save something to my long-term notes\n"
    "  /mail <workspace> - latest email (add 'unread', or 'read <id>'); 'check my email' = all\n"
    "  (ask me to reply or write email - I open it in Thunderbird for you to send)\n"
    "  /checkin - VA check-in: both inboxes, both boards, calendar, backup, sync, laptop\n"
    "  remind me at 3pm to ... | /reminders (list) | /reminders remove <text>\n"
    "  what needs my decision? | /claude <task> | find <file name> | /calendar | /books\n"
    "  /search <web query> | /backup-valerie | voice on / voice off (or the Voice button)\n"
    "  STOP button or Esc = stop what I'm doing | Pause = hold listening & scheduled tasks\n"
    "  Talk mode: say 'cancel' during the 3-second countdown, or 'Valerie, stop' while I think\n"
    "  /relay <directive> - send a directive to the right board   /va-reply <which email>\n"
    "  /sync - two-way Drive sync    /sync preview - see changes first    /sync status\n"
    "  /kb - knowledge base size     /reindex - re-read your documents\n"
    "  Ask me to create or edit a skill or knowledge note - I ask before changing anything.\n"
    "  /clear - forget this conversation     /help - this list"
)

CAPABILITY_QUESTIONS = {
    "what can you do", "what can you do for me", "what do you do", "capabilities",
    "your capabilities", "what are your capabilities", "what are your features",
    "features", "what can valerie do", "help",
}


class ChatInput(tk.Text):
    """Multi-line message box that grows with the text (2 to 10 lines, then
    scrolls), like Claude's chat input. Enter sends, Shift+Enter = new line.
    Keeps the small part of tk.Entry's API the rest of the GUI uses."""

    MIN_LINES, MAX_LINES = 2, 10

    def __init__(self, master, **kw):
        super().__init__(master, height=self.MIN_LINES, width=1, wrap=tk.WORD, undo=True, **kw)
        self.bind("<KeyRelease>", lambda e: self._resize())
        self.bind("<<Paste>>", lambda e: self.after(10, self._resize))
        self.bind("<Configure>", lambda e: self._resize())

    @staticmethod
    def _idx(i):
        return "1.0" if i in (0, "0") else ("end-1c" if i == tk.END else i)

    def get(self, first="1.0", last="end-1c"):  # Entry-style: whole text by default
        return super().get(first, last)

    def delete(self, first, last=None):
        super().delete(self._idx(first), "end" if last == tk.END else (self._idx(last) if last is not None else None))
        self._resize()

    def insert(self, index, text, *tags):
        super().insert(self._idx(index), text, *tags)
        self._resize()

    def icursor(self, index):
        self.mark_set("insert", self._idx(index))
        self.see("insert")

    def _resize(self):
        try:
            lines = self.count("1.0", "end-1c", "displaylines")
            n = (lines[0] if isinstance(lines, tuple) else lines) or 0
        except tk.TclError:
            n = int(self.index("end-1c").split(".")[0])
        n = max(self.MIN_LINES, min(self.MAX_LINES, n + 1))
        if int(self.cget("height")) != n:
            self.config(height=n)


def time_greeting(now=None):
    """Morning until noon, afternoon noon-5 pm, evening from 5 pm (and the
    small hours before 4 am). One rule for every greeting Valerie speaks."""
    h = (now or datetime.now()).hour
    if 4 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    return "Good evening"


def checkin_label(now=None):
    h = (now or datetime.now()).hour
    return "Morning" if 4 <= h < 12 else "Midday" if 12 <= h < 17 else "Evening"


def _piper_bin():
    for candidate in (BUNDLED_PIPER, VENV_PIPER):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("piper")


class ValerieAssistantGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Valerie Local Assistant")
        self.root.geometry("560x860")
        self.root.configure(bg=BG)

        self.image_path = os.path.join(cfg_mod.SYSTEM_DIR, "imgs/valerie.png")
        self.bot_name = "Valerie"
        try:
            cfg_mod.seed_user_data()   # first run: copy default skills/notes/connectors
        except Exception:
            pass
        self.config = cfg_mod.load_config()
        self.history = []                      # kept for the "both" focus
        self.histories = {wid: [] for wid in ws.all_ids(self.config)}
        self.histories["both"] = self.history
        self.focus = self.config.get("focus", "both")
        if self.focus != "both" and self.focus not in ws.all_ids(self.config):
            self.focus = "both"
        self.busy = False
        self.awaiting_calendar_reply = False
        self.awaiting_eco = False
        self.awaiting_next_eco = None
        self.speech_proc = None
        self.recorder = listen.Recorder()
        self.is_recording = False
        self.setup_ui()

        import tools as tools_mod
        tools_mod.CONFIRM = self.confirm_threadsafe

        from brain import warm_up
        threading.Thread(target=warm_up, args=(self.config,), daemon=True).start()

        if self.config.get("speech_input", {}).get("enabled", True) and listen.available():
            size = self.config.get("speech_input", {}).get("model_size", "tiny")
            threading.Thread(target=listen.preload, args=(size,), daemon=True).start()

        self.talk_mode = False
        self.talk_stop = threading.Event()
        self.paused = False
        self._job_seq = 0
        self._cancelled_upto = 0
        self._pending_talk = None
        self.dictating = False
        self.dictate_stop = threading.Event()
        self.last_mail = []      # [(account, id)] in the order last shown
        self.last_email = None   # (account, message, cleaned body) last read
        self.root.bind("<Escape>", self.cancel_current)
        self.root.after(800, self.welcome_protocol)
        if self.config.get("assistant", {}).get("talk_mode_on_start", True) and listen.available():
            self.root.after(1200, self.start_talk_mode)
        try:
            import reminders
            reminders.migrate_to_segmented()
        except Exception:
            pass
        self.root.after(15000, self._schedule_tick)

    # ---------------------------------------------------------- Workspaces
    def ws_ids(self):
        return ws.all_ids(self.config)

    def ws_label(self, wid):
        return ws.label(self.config, wid)

    def owner_suffix(self):
        o = ws.owner(self.config)
        return f", {o}" if o else ""

    def confirm_threadsafe(self, title, text):
        """Ask the owner Yes/No on the Tk thread; callable from worker threads."""
        result = {}
        done = threading.Event()

        def ask():
            try:
                result["ok"] = messagebox.askyesno(title, text, parent=self.root, icon="warning")
            finally:
                done.set()

        self.root.after(0, ask)
        done.wait()
        return result.get("ok", False)

    # ------------------------------------------------------------ Voice

    def stop_speaking(self):
        old_piper = getattr(self, "piper_proc", None)
        if old_piper and old_piper.poll() is None:
            old_piper.kill()
        if self.speech_proc and self.speech_proc.poll() is None:
            self.speech_proc.terminate()

    def speak(self, text, force=False):
        """Neural voice via Piper; espeak-ng fallback. No shell is used,
        so nothing in the model's reply can be executed."""
        if not force and not self.config.get("assistant", {}).get("speak_replies", True):
            return
        text = mail_format.speech_clean(text)[:1500]
        if not text:
            return
        try:
            if self.speech_proc and self.speech_proc.poll() is None:
                self.speech_proc.terminate()
            # Also stop the previous voice generator, or old ones pile up and
            # starve the new one of CPU (Read aloud then seems to do nothing).
            old_piper = getattr(self, "piper_proc", None)
            if old_piper and old_piper.poll() is None:
                old_piper.kill()
            voice_cfg = self.config.get("voice", {})
            onnx_path, _ = voices.voice_files(voice_cfg.get("model", "en_US-amy-medium"))
            if not os.path.exists(onnx_path):
                onnx_path, _ = voices.voice_files("en_US-amy-medium")
            piper_bin = _piper_bin()
            if os.path.exists(onnx_path) and piper_bin and shutil.which("aplay"):
                env = dict(os.environ, LD_LIBRARY_PATH=f"{PIPER_DIR}:{os.environ.get('LD_LIBRARY_PATH', '')}")
                piper = subprocess.Popen(
                    [piper_bin, "--model", onnx_path,
                     "--length_scale", str(voice_cfg.get("length_scale", 1.0)),
                     "--noise_scale", str(voice_cfg.get("noise_scale", 0.667)),
                     "--noise_w", str(voice_cfg.get("noise_w", 0.8)),
                     "--output-raw"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
                aplay = subprocess.Popen(["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-t", "raw"],
                                         stdin=piper.stdout, stderr=subprocess.DEVNULL)
                piper.stdout.close()
                piper.stdin.write(text.encode("utf-8"))
                piper.stdin.close()
                self.speech_proc = aplay
                self.piper_proc = piper
            elif shutil.which("espeak-ng"):
                self.speech_proc = subprocess.Popen(["espeak-ng", "-v", "en+f3", "-s", "145", text],
                                                    stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"Vocal Error: {e}")

    # ----------------------------------------------------------- Chat flow

    def is_speaking(self):
        return bool(self.speech_proc and self.speech_proc.poll() is None)

    # -------------------------------------------------------- Talk mode

    EXIT_PHRASES = ("stop listening", "talk mode off", "stop talk mode", "goodbye valerie", "show the chat", "show chat")

    def start_talk_mode(self):
        if self.talk_mode or not listen.available():
            return
        if self.dictating:
            self.dictate_stop.set()
        if self.paused:
            self.toggle_pause()
        self.talk_mode = True
        self.talk_stop.clear()
        self.chat_frame.pack_forget()
        self.input_row.pack_forget()
        self.voice_panel.pack(padx=16, pady=(10, 0), fill=tk.BOTH, expand=True, before=self.bottom)
        self.talk_btn.config(text="Show chat (stop Talk mode)", bg=AMBER)
        self.mic_btn.pack_forget()
        self.talk_btn.pack_forget()
        self.talk_btn.pack(side=tk.LEFT, in_=self.voice_row, fill=tk.X, expand=True)
        self._voice_state("Starting...")
        threading.Thread(target=self._talk_loop, daemon=True).start()

    def stop_talk_mode(self):
        self.talk_mode = False
        self.talk_stop.set()
        self.voice_panel.pack_forget()
        self.input_row.pack(fill=tk.X, padx=16, before=self.voice_row)
        self.talk_btn.pack_forget()
        self.mic_btn.pack(side=tk.LEFT, in_=self.voice_row, fill=tk.X, expand=True)
        self.talk_btn.pack(side=tk.LEFT, in_=self.voice_row, fill=tk.X, expand=True, padx=(6, 0))
        self.chat_frame.pack(padx=16, pady=(10, 0), fill=tk.BOTH, expand=True, before=self.bottom)
        self.talk_btn.config(text="Talk mode (hands-free)", bg=HEADER)
        self.chat_log.see(tk.END)

    def toggle_talk_mode(self):
        self.stop_talk_mode() if self.talk_mode else self.start_talk_mode()

    def _voice_state(self, text):
        self.voice_status.config(text=text)

    def _talk_loop(self):
        import time
        size = self.config.get("speech_input", {}).get("model_size", "tiny")
        while self.talk_mode and not self.talk_stop.is_set():
            # never listen while she's talking (she'd hear herself) or paused
            if self.is_speaking() or self.paused:
                time.sleep(0.3)
                continue
            time.sleep(0.4)  # let the room settle after she stops speaking
            if self.is_speaking() or self.paused:
                continue
            if not self.busy:
                self.root.after(0, lambda: self._voice_state("Listening... (just talk)"))
            audio = listen.listen_utterance(
                self.talk_stop, on_state=lambda t: self.root.after(0, lambda: self._voice_state(t)),
                abort_if=self.is_speaking)
            if self.is_speaking():
                continue
            if audio is None or not self.talk_mode:
                continue
            self.root.after(0, lambda: self._voice_state("Got it - transcribing..."))
            text = listen.transcribe(audio, model_size=size, strict=True).strip()
            if not text or text.startswith("[speech recognition error"):
                continue
            if self.busy:   # while she's thinking, only "stop"/"cancel" matters
                if self.CANCEL_WORDS.match(text):
                    self.root.after(0, self.cancel_current)
                continue
            if self._is_noise_phrase(text):
                self.root.after(0, lambda t=text: self._voice_state(f"(ignored \"{t[:30]}\" - sounded like noise)"))
                time.sleep(1.2)
                continue
            self.root.after(0, lambda t=text: self._talk_heard(t))
            time.sleep(0.5)

    SHORT_OK = {"yes", "no", "yeah", "nope", "stop", "help", "thanks", "okay", "ok"}
    NOISE_PHRASES = {"you", "thank you", "thank you.", "thanks for watching", "bye", "together", "so", "uh",
                     "um", "hmm", "the", "a", "oh", "i", "and", "music", "applause", "silence"}

    def _is_noise_phrase(self, text):
        """Single stray words (outside a few real one-word answers) are almost
        always background noise misheard as speech - don't send them to the AI."""
        clean = re.sub(r"[^a-z' ]", " ", text.lower()).split()
        if not clean:
            return True
        phrase = " ".join(clean)
        if phrase in self.SHORT_OK:
            return False
        return phrase in self.NOISE_PHRASES or len(clean) < 2

    # ------------------------------------------------- Click-to-dictate

    def toggle_dictation(self):
        """Click: listen, auto-stop on a pause (or click again), then put the
        words in the text box to check before sending - never auto-sends."""
        if self.talk_mode:
            return
        if self.dictating:
            self.dictate_stop.set()
            self.dictate_btn.config(text="...", bg="#999999")
            return
        if not listen.available():
            self.update_chat(self.bot_name, "The microphone isn't available (missing: " + ", ".join(listen.missing_pieces()) + ").", speak=False)
            return
        if self.speech_proc and self.speech_proc.poll() is None:
            self.speech_proc.terminate()  # don't record Valerie's own voice
        self.dictating = True
        self.dictate_stop.clear()
        self.dictate_btn.config(text="Listening... click to stop", bg="#CC3333")
        size = self.config.get("speech_input", {}).get("model_size", "tiny")

        def state(t):
            self.root.after(0, lambda: self.dictating and self.dictate_btn.config(
                text="Hearing you... click to stop" if t.startswith("Hearing") else t))

        def work():
            audio = listen.listen_utterance(self.dictate_stop, silence_seconds=1.5, on_state=state, keep_on_stop=True)
            if audio is None:
                return ""
            self.root.after(0, lambda: self.dictate_btn.config(text="Transcribing...", bg="#999999"))
            return listen.transcribe(audio, model_size=size, strict=True)

        def done(text):
            self.dictating = False
            self.dictate_btn.config(text="MIC", bg=HEADER)
            text = (text or "").strip()
            if not text:
                self.status.config(text="Didn't catch anything - click MIC and try again.")
                self.root.after(4000, self._refresh_status)
                return
            if text.startswith("[speech recognition error"):
                self.update_chat(self.bot_name, text, speak=False)
                return
            current = self.user_input.get().rstrip()
            self.user_input.delete(0, tk.END)
            self.user_input.insert(0, f"{current} {text}".strip())
            self.user_input.focus_set()
            self.user_input.icursor(tk.END)

        self._in_background(work, done)

    def _talk_heard(self, text):
        # "cancel"/"stop" during the send countdown drops what was heard
        if self._pending_talk is not None and self.CANCEL_WORDS.match(text):
            self.cancel_current()
            return
        self.voice_you.config(text=f"You: {text}")
        tcfg = self.config.get("talk", {})
        if tcfg.get("wake_word", True):
            import time as _t
            idle = _t.time() - getattr(self, "_last_talk_reply", 0) > tcfg.get("follow_up_seconds", 60)
            wake = re.match(r"^\W*(hey |hi |ok |okay )?(valerie|valery|valeri|valarie|vallery|valeria|val)\b[\s,.!?]*", text, re.I)
            if idle and not wake:
                self._voice_state('Listening... (say "Valerie, ..." to get my attention)')
                return
            if wake:
                text = text[wake.end():].strip() or "yes?"
        if any(p in text.lower() for p in self.EXIT_PHRASES):
            self.update_chat("You", text)
            self.stop_talk_mode()
            self.update_chat(self.bot_name, "Talk mode off - I'm back in the chat window.", speak=False)
            return
        if self.CANCEL_WORDS.match(text) and not self.busy:
            self._voice_state("Listening...")
            return
        # 3-second window to cancel a misheard phrase before anything runs
        self._pending_talk = text
        self._countdown(3, text)

    def _countdown(self, n, text):
        if self._pending_talk != text:
            return
        if n > 0:
            self._voice_state(f"Heard: \u201c{text[:60]}\u201d\nSending in {n}... (say 'cancel' or press Esc)")
            self.voice_cancel_btn.pack(pady=(4, 0))
            self.root.after(1000, lambda: self._countdown(n - 1, text))
            return
        self.voice_cancel_btn.pack_forget()
        self._pending_talk = None
        self._voice_state("Thinking...")
        self.user_input.delete(0, tk.END)
        self.user_input.insert(0, text)
        self.on_send()

    def welcome_protocol(self):
        greeting = time_greeting()
        try:   # home-system check from the local status collector, if installed (offline-safe)
            import va_network
            home = va_network.home_summary()
            if home:
                self.update_chat(self.bot_name, "Home system: " + home)
        except Exception:
            pass
        ids = self.ws_ids()
        if not ids:
            self.update_chat(self.bot_name, f"{greeting}{self.owner_suffix()}. Type /help to see what I can do, "
                             "or open Settings > Workspaces to set up your mail.")
            return
        self.update_chat(self.bot_name, f"{greeting}{self.owner_suffix()}. Checking your inbox{'es' if len(ids) > 1 else ''}...", speak=False)

        def count():
            import mail_router
            unread = {}
            for acct in ids:
                try:
                    _src, msgs, _ = mail_router.list_messages(self.config, acct, "INBOX", limit=15)
                    unread[acct] = sum(1 for m in msgs if m["unread"])
                except Exception:
                    unread[acct] = None
            return unread

        def ask(unread):
            self._set_busy(False)
            if not isinstance(unread, dict):
                unread = {}
            parts = [f"{unread[a]} unread {self.ws_label(a)} email{'s' if unread[a] != 1 else ''}"
                     for a in ids if unread.get(a) is not None]
            missing = [self.ws_label(a) for a in ids if unread.get(a) is None]
            if parts:
                counts = "You have " + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]) + "."
            else:
                counts = ""
            if missing:
                counts += (" " if counts else "") + (f"I couldn't reach the {' or '.join(missing)} mailbox"
                                                     f"{'es' if len(missing) > 1 else ''} - check Settings > Workspaces.")
            if len(ids) == 1:
                question = f"Would you like your {self.ws_label(ids[0])} report?"
                hint = "(say Yes or Later)"
            else:
                names = [self.ws_label(i) for i in ids]
                question = ("Which workspace would you like me to report on first: "
                            + (", ".join(names[:-1]) + " or " + names[-1]) + "?")
                hint = f"(say {', '.join(names)}, All, or Later)"
            self.awaiting_eco = True
            self._report_queue = []
            self.update_chat(self.bot_name, f"{counts}\n{question}\n{hint}".strip(), speak=False)
            self.speak(f"{greeting}{self.owner_suffix()}. {counts} {question}")

        self._set_busy(True)
        self._in_background(count, ask)

    def _sounds_like_workspace(self, lower):
        """Speech recognition often mishears a one-word answer ("after" or
        "offer" for Author, "couching" for Coaching). Only for short replies
        to Valerie's own question: match per-workspace "sounds_like" words
        from config, then a close spelling match to the workspace name."""
        import difflib
        words = re.findall(r"[a-z]+", lower)
        if not words or len(words) > 3:
            return None
        hits = set()
        for wid in self.ws_ids():
            w = ws.get(self.config, wid)
            alikes = [x.lower() for x in w.get("sounds_like", [])]
            names = [w.get("label", wid).lower()] + [k.lower() for k in w.get("keywords", []) if " " not in k]
            for word in words:
                if word in alikes or difflib.get_close_matches(word, names, n=1, cutoff=0.72):
                    hits.add(wid)
        return hits.pop() if len(hits) == 1 else None

    def _eco_choice(self, lower):
        ids = self.ws_ids()
        if re.search(r"\b(both|all|everything|each|every one)\b", lower):
            return "both"
        if len(ids) == 1 and re.fullmatch(r"(yes|yeah|yep|sure|ok|okay|please|go ahead|yes please)", lower):
            return ids[0]
        hit = ws.detect(self.config, lower) or ws.detect_last(self.config, lower)
        if hit:
            return hit
        hit = self._sounds_like_workspace(lower)
        if hit:
            return hit
        if re.fullmatch(r"(no|nope|later|not now|skip|neither|no thanks|maybe later)", lower):
            return "none"
        return None

    def report_ecosystem(self, eco, then=None, scheduled=False):
        """One ecosystem's report: email (readable), its board, and what's
        waiting on the owner - read aloud. `then` = what to offer/do next."""
        import mail_format
        acct, board = eco, ws.board(self.config, eco)
        label = self.ws_label(eco)
        self.set_focus(eco)
        eco_tz = ws.tz(self.config, eco, None)

        def work():
            import connectors as conn
            import mail_router
            import va_extras
            shown, spoken, msgs = [f"=== {label.upper()} REPORT ==="], [], []
            if self.config.get("daily_report", {}).get("workspace") == eco:
                d, fresh, rep = va_extras.daily_report(self.config)
                if d:
                    st = re.search(r"STATUS:\s*(\w+)", rep)
                    shown.append(f"Daily ecosystem report ({d}{'' if fresh else ' - STALE, today’s not in yet'}):\n"
                                 + rep.strip()[:1500] + ("..." if len(rep) > 1500 else ""))
                    if fresh:
                        spoken.append(f"Today's ecosystem report says status {st.group(1).lower() if st else 'unknown'}.")
                    else:
                        spoken.append(f"Today's ecosystem report isn't in yet. The last one is from {d}, so I'm not using it.")
            try:
                src, msgs, _ = mail_router.list_messages(self.config, acct, "INBOX", limit=8)
                self.last_mail = [(acct, m["id"]) for m in msgs]
                unread = [m for m in msgs if m["unread"]]
                shown.append(f"Email ({src}): {len(unread)} unread")
                shown += [mail_format.list_line(m, eco_tz) for m in msgs[:8]]
                spoken.append(mail_format.spoken_list(f"{label} email", msgs, tzname=eco_tz))
            except Exception as e:  # noqa: BLE001
                shown.append(f"Email: unavailable - {e}")
                spoken.append(f"I couldn't check the {label} email.")
            if board:
                try:
                    r = conn.run(board, "inbox", {"limit": 6}, cfg=self.config)
                    shown.append("Board: " + r.replace("\n", "\n  "))
                    n = r.count("[row ")
                    spoken.append(f"{n} open board items." if n else "Nothing open on the board.")
                except Exception as e:  # noqa: BLE001
                    shown.append(f"Board: {str(e)[:200]}")
            try:
                waiting = []
                for m in msgs[:8]:
                    full = mail_router.read_message(self.config, acct, m["id"])
                    for ln in va_extras._decision_lines(mail_format.clean_body(full.get("body", "")), ws.owner(self.config) or None):
                        waiting.append(ln[:220])
                waiting = list(dict.fromkeys(waiting))[:5]
                if waiting:
                    shown.append("Waiting on you:\n" + "\n".join(f"- {w}" for w in waiting))
                    spoken.append("Waiting on you: " + ". ".join(w[:160] for w in waiting[:3]))
                else:
                    spoken.append("Nothing in the recent email is waiting on your decision.")
            except Exception:
                pass
            return "\n".join(shown), " ".join(spoken)

        def done(r):
            self._set_busy(False)
            if isinstance(r, str):
                self.update_chat(self.bot_name, r)
                return
            text, voice = r
            self.update_chat(self.bot_name, text + "\n\nSay 'read the first one' to open an email.", speak=False, voice=voice)
            if not scheduled:
                self._file_reported(acct, [i for a, i in self.last_mail if a == acct])
            done_set = getattr(self, "_reported", set())
            done_set.add(eco)
            self._reported = done_set
            remaining = [w for w in self.ws_ids() if w not in done_set]
            if then == "next" and remaining:
                self.speak(voice)
                self.report_ecosystem(remaining[0], then="next")
                return
            if then == "offer" and remaining:
                self.awaiting_next_eco = remaining[0]
                q = f"Want the {self.ws_label(remaining[0])} report next?"
                self.update_chat(self.bot_name, q, speak=False)
                self.speak(f"{voice} {q}")
            else:
                self.speak(voice)

        self._set_busy(True)
        self.update_chat(self.bot_name, f"...putting together the {label} report...", speak=False)
        self._in_background(work, done)

    def on_send(self, event=None):
        query = self.user_input.get().strip()
        if not query or self.busy:
            return
        self.update_chat("You", query)
        self.user_input.delete(0, tk.END)
        lower = query.lower().strip(" .!?")

        # Reply to "which ecosystem first?" (greeting) or "want the other one next?"
        if getattr(self, "awaiting_next_eco", None):
            other, self.awaiting_next_eco = self.awaiting_next_eco, None
            choice = self._eco_choice(lower)
            if re.fullmatch(r"(yes|yeah|yep|sure|ok|okay|please|go ahead|yes please)", lower) or choice == other:
                self.report_ecosystem(other)
                return
            if choice == "none" or lower in ("no", "n"):
                self.update_chat(self.bot_name, "Okay. What can I do for you?")
                return
        if getattr(self, "awaiting_eco", False):
            self.awaiting_eco = False
            choice = self._eco_choice(lower)
            self._reported = set()
            if choice in self.ws_ids():
                self.report_ecosystem(choice, then="offer")
                return
            if choice == "both" and self.ws_ids():
                self.report_ecosystem(self.ws_ids()[0], then="next")
                return
            if choice == "none":
                self.update_chat(self.bot_name, "Okay - just ask whenever you want a report. What can I do for you?")
                return

        if not query.startswith("/"):
            self._detect_focus(lower)
        m = re.fullmatch(r"(focus|switch) (on |to )?(the )?(.+?)( workspace| ecosystem)?", lower)
        if m:
            target = "both" if m.group(4) in ("both", "all", "everything") else ws.detect(self.config, m.group(4))
            if target:
                self.set_focus(target, announce=True)
                return

        if query.startswith("/"):
            self.handle_command(query)
            return

        if re.fullmatch(r"(good )?(morning|afternoon|evening)( valerie)?|(hi|hello|hey) valerie|good morning,? valerie", lower):
            self.welcome_protocol()
            return
        import tv_control
        tv = tv_control.parse(lower)
        if tv:
            action, app, times = tv

            def tv_done(r):
                self.update_chat(self.bot_name, r.replace("Not done: ", "I couldn't do that: "))

            self._in_background(lambda: self._run_local_tool("tv", {"action": action, "app": app, "times": times}), tv_done)
            return
        if re.match(r"(please )?(remind me|set a reminder|reminder)\b", lower):
            self.local_reminder(query)
            return
        if re.search(r"\b(what('?s| is)? (waiting|pending) on me|needs? my (decision|approval)|"
                     r"what needs (my|me|a) (decision|approval|attention)|waiting on me|decisions? (list|digest)|my decisions)\b", lower):
            self.handle_command("/decisions")
            return
        if re.search(r"\b(my )?(calendar|schedule|agenda)\b.*\b(today|tomorrow|this week)\b|what'?s on my (calendar|schedule)", lower) \
                and "remind" not in lower:
            self.handle_command("/calendar " + ("7" if "week" in lower else "2" if "tomorrow" in lower else "1"))
            return
        m = re.match(r"(find|where('?s| is| are)|locate|search for)\s+(my |the )?(file |files |folder )?(.+?)\??$", lower)
        if m and not re.search(r"\b(e-?mails?|inbox|web|online|internet)\b", lower):
            self.handle_command("/find " + m.group(5))
            return
        if re.search(r"\b(kdp|book projects?|books)\b.*\b(status|tracker|progress|ready|closest)\b", lower):
            self.handle_command("/kdp")
            return
        m = re.match(r"(ask claude|hey claude|claude,)\s*(to |about |:)?\s*(.+)", lower)
        if m:
            self.handle_command("/claude " + query[len(query) - len(m.group(3)):])
            return
        m = re.match(r"(search the web for|web search|look up online|google)\s+(.+)", lower)
        if m:
            self.handle_command("/search " + m.group(2))
            return
        if lower in ("voice off", "mute", "stop talking", "be quiet", "quiet"):
            if self.config.get("assistant", {}).get("speak_replies", True):
                self.toggle_voice()
            self.stop_speaking()
            return
        if lower in ("voice on", "unmute", "talk to me"):
            if not self.config.get("assistant", {}).get("speak_replies", True):
                self.toggle_voice()
            self.update_chat(self.bot_name, "Voice is on.")
            return

        if lower in ("system check", "check system", "status check"):
            self.update_chat(self.bot_name, self._run_local_tool("system_check"))
            return

        m = re.fullmatch(r"(give me |run |show me |read me )?(the |my )?(.+?) (report|update|rundown|briefing)", lower)
        if m and ws.detect(self.config, m.group(3)):
            self._reported = set()
            self.report_ecosystem(ws.detect(self.config, m.group(3)), then="offer")
            return

        if lower in ("undo", "undo that", "bring them back", "restore them", "put them back", "undo delete"):
            self.undo_filing()
            return

        if lower in CAPABILITY_QUESTIONS:
            self.show_capabilities()
            return

        if re.search(r"\bsummari[sz]e (it|that|this|the (e-?mail|message))\b", lower) and self.last_email:
            self.summarize_last_email()
            return

        intent = self._read_intent(lower)
        if intent:
            self.read_email(*intent)
            return

        if self._is_mail_check(lower) or re.search(r"\bread (me )?(my )?(e-?mails?|mail|inbox)\b", lower):
            self.quick_mail_check(lower)
            return

        self.call_brain(query)

    def _is_mail_check(self, lower):
        """Plain 'check my email' requests are answered straight from
        Thunderbird (seconds) instead of through the slow local model.
        Anything that writes, replies, deletes or marks still goes to the AI."""
        if not re.search(r"\b(e-?mails?|mail|inbox(es)?)\b", lower):
            return False
        if re.search(r"\b(reply|respond|send|write|draft|compose|forward|delete|remove|trash|mark|answer)\b", lower):
            return False
        return bool(re.search(r"\b(check|any|new|unread|show|list|what|findings|latest|recent|got|look)\b", lower))

    def quick_mail_check(self, lower, on_finished=None):
        hit = ws.detect(self.config, lower)
        accounts = [hit] if hit else self.ws_ids()
        unread_counts = {a: 0 for a in self.ws_ids()}
        spoken = []

        def work():
            import mail_router
            import thunderbird_mail as tbm
            parts, order = [], []
            for acct in accounts:
                label = self.ws_label(acct)
                try:
                    src, msgs, note = mail_router.list_messages(self.config, acct, "INBOX", limit=8)
                    unread_counts[acct] = sum(1 for m in msgs if m["unread"])
                    order += [(acct, m["id"]) for m in msgs]
                    lines = [mail_format.list_line(m) for m in msgs] or ["(no email downloaded)"]
                    import mail_client
                    parts.append(f"{mail_client.account_label(self.config, acct)} - {unread_counts[acct]} unread (from {src})"
                                 + (f" {note}" if note else "") + "\n" + "\n".join(lines))
                    spoken.append(mail_format.spoken_list(f"{label} inbox", msgs))
                except Exception as e:  # noqa: BLE001
                    parts.append(f"{acct}: {e}")
                    spoken.append(f"I couldn't check the {label} inbox.")
            self.last_mail = order
            if not accounts:
                return "No mailboxes are set up yet - open Settings > Workspaces."
            return ("\n\n".join(parts) + "\n\nSay or type 'read the first one', 'read the latest <workspace> email', "
                    "or 'read email t3'. After reading, say 'summarize it'.")

        def done(r):
            self._set_busy(False)
            self.update_chat(self.bot_name, r, speak=False)
            if on_finished:
                on_finished(unread_counts)
                return
            self.speak(" ".join(spoken))
            for a in accounts:
                self._file_reported(a, [i for x, i in self.last_mail if x == a])

        self._set_busy(True)
        self._in_background(work, done)

    # ----------------------------------------------------- Reading email

    _ORDINALS = {"first": 0, "1st": 0, "top": 0, "latest": 0, "newest": 0, "last": 0, "most recent": 0,
                 "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}

    def _read_intent(self, lower):
        """'read the first one', 'read the latest <workspace> email', 'read email t3',
        'open the second author message' -> (account or None, index or None, id or None)."""
        if not re.search(r"\b(read|open|play)\b", lower):
            return None
        mid = re.search(r"\b(?:id\s*)?([st]\d+)\b", lower)
        acct = ws.detect(self.config, lower)
        idx = None
        for word, i in self._ORDINALS.items():
            if re.search(rf"\b{word}\b", lower):
                idx = i
                break
        if mid:
            return acct, None, mid.group(1)
        if idx is not None and re.search(r"\b(e-?mail|mail|message|one|it)\b", lower):
            return acct, idx, None
        return None

    def read_email(self, account, index=None, msg_id=None):
        def work():
            import mail_router
            acct, mid = account, msg_id
            if mid is None:
                pool = [x for x in self.last_mail if acct is None or x[0] == acct]
                if not pool:  # nothing listed yet - list that inbox first
                    a = acct or (self.ws_ids() or [None])[0]
                    _src, msgs, _ = mail_router.list_messages(self.config, a, "INBOX", limit=8)
                    pool = [(a, m["id"]) for m in msgs]
                if not pool or index >= len(pool):
                    return None, "There's no email at that position."
                acct, mid = pool[index]
            if acct is None:
                acct = next((a for a, i in self.last_mail if i == mid), (self.ws_ids() or [None])[0])
            m = mail_router.read_message(self.config, acct, mid)
            m["id"] = mid
            shown, body = mail_format.readable_email(m)
            self.last_email = (acct, m, body)
            label = self.ws_label(acct).upper()
            return mail_format.spoken_email(m, body), f"[{label} email]\n{shown}"

        def done(result):
            self._set_busy(False)
            if isinstance(result, str):  # error text from _in_background
                self.update_chat(self.bot_name, result)
                return
            spoken, shown = result
            if spoken is None:
                self.update_chat(self.bot_name, shown)
                return
            self.update_chat(self.bot_name, shown, speak=False)
            self.speak(spoken)
            if self.last_email:
                acct_, m_, _b = self.last_email
                self._file_reported(acct_, [m_.get("id") or ""] if m_.get("id") else [])

        self._set_busy(True)
        self.update_chat(self.bot_name, "...opening the email...", speak=False)
        self._in_background(work, done)

    def _file_reported(self, acct, ids):
        """Mark reported emails read and move them to Trash (keeping any that
        are waiting on the owner), archiving a text copy first. Background."""
        mode = self.config.get("mail_after_report", "trash")
        if mode == "off":
            return
        server_ids = [i for i in ids if str(i).startswith("s")]
        if not server_ids:
            return

        def work():
            import mail_client
            import mail_router
            import va_extras
            arch_dir = os.path.join(cfg_mod.DATA_DIR, "mail-archive", acct)
            os.makedirs(arch_dir, exist_ok=True)
            trash, keep = [], []
            for mid in server_ids:
                try:
                    m = mail_router.read_message(self.config, acct, mid)
                except Exception:
                    continue
                if m.get("source") != "server":
                    continue
                shown, body = mail_format.readable_email(m)
                if va_extras._decision_lines(body, ws.owner(self.config) or None):
                    keep.append(mid[1:])
                    continue
                trash.append(mid[1:])
                with open(os.path.join(arch_dir, datetime.now().strftime("%Y-%m-%d") + ".md"), "a", encoding="utf-8") as fh:
                    fh.write(f"\n---\n{shown}\n")
            if mode == "read":
                keep, trash = keep + trash, []
            moved = mail_client.file_batch(self.config, acct, trash, keep)
            return len(trash), len(keep), moved

        def done(r):
            if not isinstance(r, tuple):
                self.update_chat(self.bot_name, f"(Couldn't file the reported {acct} emails: {str(r)[:150]})", speak=False)
                return
            n_trash, n_keep, moved = r
            if moved:
                self.last_filed = (acct, moved)
            bits = []
            if n_trash:
                bits.append(f"moved {n_trash} to Trash")
            if n_keep:
                bits.append(f"kept {n_keep} that need{'s' if n_keep == 1 else ''} you (marked read)")
            if bits:
                self.update_chat(self.bot_name, f"({acct.title()} inbox: {', '.join(bits)}."
                                 + (" Say 'undo' to bring them back.)" if n_trash else ")"), speak=False)

        self._in_background(work, done, keep=True)

    def undo_filing(self):
        if not getattr(self, "last_filed", None):
            self.update_chat(self.bot_name, "There's nothing to undo.")
            return
        acct, mids = self.last_filed
        self.last_filed = None

        def work():
            import mail_client
            return mail_client.restore_from_trash(self.config, acct, mids)

        self._in_background(work, lambda n: self.update_chat(
            self.bot_name, f"Brought {n} email{'s' if n != 1 else ''} back to the {acct} inbox." if isinstance(n, int) else str(n)))

    def summarize_last_email(self):
        if not self.last_email:
            self.update_chat(self.bot_name, "Open an email first - say 'read the first one'.")
            return
        acct, m, body = self.last_email
        prompt = (f"Summarize this {self.ws_label(acct)} email for {ws.owner(self.config) or 'me'} in 3 to 5 short sentences that sound natural read aloud. "
                  f"Lead with anything that needs his decision or action. No lists or symbols.\n\n"
                  f"From: {mail_format.sender(m.get('from'))}\nSubject: {mail_format.readable_subject(m.get('subject'))}\n\n{body[:5000]}")
        self.call_brain(prompt, shown=f"summarize the email '{mail_format.readable_subject(m.get('subject'))}'")

    def run_checkin(self, scheduled=False, ecosystems=None):
        """The routine VA round, done locally in seconds (no AI call):
        both inboxes, both boards, backup, sync and the laptop - with the
        workspaces reported separately."""
        ecosystems = tuple(ecosystems or self.ws_ids())
        label = checkin_label()
        counts = {"board_items": 0, "problems": 0}
        counts.update({f"{w}_unread": 0 for w in ecosystems})

        def work():
            import connectors as conn
            import mail_router
            import tools as t
            out = [f"{label.upper()} VA CHECK-IN - {datetime.now().strftime('%a %b %d, %I:%M %p')}"]
            for acct in ecosystems:
                eco, board = self.ws_label(acct).upper(), ws.board(self.config, acct)
                out.append(f"\n=== {eco} ===")
                try:
                    src, msgs, _ = mail_router.list_messages(self.config, acct, "INBOX", limit=8)
                    unread = [m for m in msgs if m["unread"]]
                    counts[f"{acct}_unread"] = len(unread)
                    counts[f"{acct}_senders"] = [m["from"].split("<")[0].strip().strip('"') for m in unread][:3]
                    out.append(f"Email ({src}): {len(unread)} unread of the latest {len(msgs)}")
                    for m in (unread or msgs[:3]):
                        out.append(f"  [id {m['id']}] {'* ' if m['unread'] else ''}{m['from'].split('<')[0].strip()}: {m['subject'][:80]}")
                except Exception as e:  # noqa: BLE001
                    counts["problems"] += 1
                    counts.setdefault("broken", []).append(f"the {self.ws_label(acct)} email")
                    out.append(f"Email: unavailable - {e}")
                if board:
                    try:
                        r = conn.run(board, "inbox", {"limit": 6}, cfg=self.config)
                        counts["board_items"] += max(0, r.count("[row "))
                        counts[f"{acct}_board"] = max(0, r.count("[row "))
                        if re.search(r"cut the board off|couldn'?t|can'?t see|error|unavailable", r, re.I) and "[row " not in r:
                            counts["problems"] += 1
                            counts.setdefault("broken", []).append(f"the {self.ws_label(acct)} board")
                        out.append("Board: " + r.replace("\n", "\n  "))
                    except Exception as e:  # noqa: BLE001
                        counts["problems"] += 1
                        counts.setdefault("broken", []).append(f"the {self.ws_label(acct)} board")
                        out.append(f"Board: {str(e)[:220]}")
                if self.config.get("daily_report", {}).get("workspace") == acct:
                    import va_extras
                    d, fresh, rep_text = va_extras.daily_report(self.config)
                    st = re.search(r"STATUS:\s*(\w+)", rep_text or "")
                    counts["report"] = (d, fresh, st.group(1).lower() if st else None)
                    if d:
                        out.append(f"Daily ecosystem report ({d}{'' if fresh else ' - STALE, today' + chr(39) + 's not in yet'}): "
                                   + (st.group(1) if st else "no status line"))
            if self.config.get("calendar", {}).get("ics_urls"):
                try:
                    import va_extras
                    out.insert(1, "Calendar - " + va_extras.calendar_events(self.config, 1))
                except Exception as e:  # noqa: BLE001
                    out.insert(1, f"Calendar: {str(e)[:120]}")
            out.append("\n=== SYSTEMS ===")
            integ = self.config.get("integrations", {})
            sys_checks = [("system_check", {}), ("disk_usage", {})]
            if integ.get("backup_log"):
                sys_checks.insert(0, ("backup_status", {}))
            if integ.get("drive_sync_script"):
                sys_checks.insert(1, ("drive_sync", {"action": "status"}))
            for name, args in sys_checks:
                out.append(f"- {t.execute_tool(name, args)[:260]}")
            return "\n".join(out)

        def done(r):
            self._set_busy(False)
            voice = self._spoken_checkin(counts, ecosystems, label, scheduled)
            self.update_chat(self.bot_name, r, speak=False, voice=voice)
            self.speak(voice)

        self._set_busy(True)
        self.update_chat(self.bot_name, f"...running the {label.lower()} check-in...", speak=False)
        self._in_background(work, done)

    def _spoken_checkin(self, counts, ecosystems, label, scheduled):
        """What Valerie SAYS after a check-in: a few natural sentences, the way
        a person would brief you - not the on-screen report read out loud."""
        words = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

        def n(x, thing):
            return f"{words.get(x, x)} {thing}{'' if x == 1 else 's'}"

        lines = [f"{time_greeting()}{self.owner_suffix()}. Here's your {label.lower()} check-in." if scheduled
                 else f"Okay, {label.lower()} check-in's done."]
        quiet = []
        for a in ecosystems:
            name, u = self.ws_label(a), counts.get(f"{a}_unread", 0)
            board = counts.get(f"{a}_board", 0)
            bits = []
            if u:
                who = counts.get(f"{a}_senders") or []
                frm = f", from {who[0]}" if u == 1 and who else ""
                bits.append(f"you've got {n(u, 'new email')}{frm}")
            if board:
                bits.append(f"{n(board, 'item')} waiting on the board")
            if bits:
                lines.append(f"On the {name} side, " + " and ".join(bits) + ".")
            else:
                quiet.append(name)
        if quiet:
            lines.append(f"{quiet[0]}'s quiet, nothing new." if len(quiet) == 1 else
                         (" and ".join(quiet) + " are both quiet." if len(quiet) == 2 else "Everything else is quiet."))
        rep = counts.get("report")
        if rep and rep[0]:
            if rep[1]:
                lines.append(f"Today's ecosystem report came in {rep[2] or 'with no status'}.")
            else:
                lines.append("Today's ecosystem report hasn't landed yet, so I'm not going off yesterday's.")
        broken = counts.get("broken") or []
        if broken:
            lines.append(f"I couldn't get into {' or '.join(broken)}, so I've left the details on screen.")
        if any(counts.get(f"{a}_unread") for a in ecosystems):
            lines.append("Want me to read you the newest one?")
        else:
            lines.append("That's everything for now.")
        return " ".join(lines)

    def show_capabilities(self):
        """Instant, always-accurate answer built from what's actually
        configured right now (no model call)."""
        cfg = self.config
        try:
            st = knowledge.stats()
            kb = f"{st['files']} document{'s' if st['files'] != 1 else ''} ({st['passages']} passages)"
        except Exception:
            kb = "your documents (index not built yet - type /reindex)"
        sk = skills_mod.load_skills()
        skill_lines = "\n".join(f"  /{s['name']} - {s['description']}" for s in sk.values()) or "  (none yet)"
        provider = cfg.get("active_provider", "ollama")
        model = cfg.get("ollama", {}).get("model") if provider == "ollama" else cfg.get(provider, {}).get("model", "")
        ids = self.ws_ids()
        ws_lines = "\n".join(f"  - {self.ws_label(w)}: {ws.mail(cfg, w).get('address') or 'no mailbox yet'}"
                              + (f", board '{ws.board(cfg, w)}'" if ws.board(cfg, w) else "")
                              + f", {ws.tz(cfg, w).split('/')[-1].replace('_', ' ')} time" for w in ids) or "  (none yet - Settings > Workspaces)"
        integ = cfg.get("integrations", {})
        optional = []
        if integ.get("backup_log"):
            optional.append("  - Backup status from your rclone log (/backup).")
        if integ.get("drive_sync_script"):
            optional.append("  - Two-way sync with your sync script (/sync, /sync preview).")
        if integ.get("book_series_dir"):
            optional.append("  - Book projects tracker (/books) and KDP cover-size calculator.")
        if integ.get("rclone_backup_remote"):
            optional.append("  - Nightly backup of my skills and notes (/backup-valerie).")
        if cfg.get("web_app", {}).get("url"):
            optional.append("  - Google Sheets & Docs via your Apps Script web app.")
        text = (
            f"Here's what I can do (brain: {provider} / {model}{', fully offline' if provider == 'ollama' else ''}):\n\n"
            "CHAT & MEMORY\n"
            "  - Talk naturally; I remember recent exchanges, separately per workspace (/clear to reset).\n"
            "  - /remember <fact> saves something to my long-term notes.\n\n"
            "KNOWLEDGE\n"
            f"  - I answer from {kb} in your knowledge folders (/kb, /reindex).\n\n"
            f"WORKSPACES (kept strictly separate)\n{ws_lines}\n"
            "  - Check-in button or /checkin: every inbox and board, plus this computer.\n"
            "  - Focus button switches between workspaces; memory and knowledge follow it.\n"
            "  - '<workspace> report', 'what needs my decision?'\n\n"
            "EMAIL (mail server or Thunderbird)\n"
            "  - List, search and read; 'read the first one'; 'summarize it'.\n"
            "  - Write and reply - nothing is sent without your Yes.\n"
            "  - Mark read, delete (to Trash), 'undo'.\n\n"
            "EVERYDAY TOOLS\n"
            "  - Reminders & schedule: 'remind me at 3pm to...', per-workspace time zones (Settings > Schedule).\n"
            "  - Find files on this computer and plugged-in drives; move files to the Trash (you approve).\n"
            "  - Calendar (private iCal link), web search, 'ask Claude ...' for hard tasks (if Claude Code is installed).\n"
            "  - Computer health and disk space; read-only file browsing.\n"
            + ("\n".join(optional) + "\n" if optional else "") + "\n"
            "SELF-UPDATING\n"
            "  - I create and edit my own skills and notes; edits and deletes wait for your Yes.\n\n"
            "SKILLS (type the command plus details)\n"
            f"{skill_lines}\n\n"
            "VOICE\n"
            "  - Voice button, Read aloud under each reply, MIC dictation, Hold to Talk, and hands-free Talk mode.\n"
            "  - STOP or Esc stops anything; Pause holds listening and scheduled tasks.\n\n"
            "Type /help for the command list."
        )
        self.update_chat(self.bot_name, text, speak=False)
        self.speak(f"I can chat with memory, answer from {kb.split(' (')[0]} in your knowledge folders, "
                   f"handle email for {len(ids)} workspace{'s' if len(ids) != 1 else ''}, run {len(sk)} skills, "
                   "set reminders, find files and search the web. The full list is on screen.")

    def _run_local_tool(self, name, args=None):
        from tools import execute_tool
        return execute_tool(name, args or {})

    def handle_command(self, text):
        cmd, _, rest = text[1:].partition(" ")
        cmd = cmd.lower()
        if cmd in ("help", "?"):
            self.update_chat(self.bot_name, HELP_TEXT, speak=False)
        elif cmd == "skills":
            self._refresh_status()
            sk = skills_mod.load_skills()
            lines = [f"/{s['name']} - {s['description']}" for s in sk.values()] or ["(no skills yet)"]
            self.update_chat(self.bot_name, "Skills:\n" + "\n".join(lines) +
                             f"\n\nAdd more as .md files in {cfg_mod.SKILLS_DIR}", speak=False)
        elif cmd == "kb":
            st = knowledge.stats()
            dirs = "\n".join(f"  {'OK ' if os.path.isdir(d) else 'missing '}{d}" for d in self.config["knowledge"]["dirs"])
            self.update_chat(self.bot_name, f"Knowledge base: {st['files']} files, {st['passages']} passages.\n{dirs}", speak=False)
        elif cmd == "reindex":
            self.update_chat(self.bot_name, "Re-reading your documents...", speak=False)
            self._in_background(lambda: knowledge.build_index(self.config),
                                lambda r: (self._refresh_status(), self.update_chat(self.bot_name, f"Done: {r['files']} files, {r['passages']} passages." if isinstance(r, dict) else r)))
        elif cmd == "sync":
            action = (rest.strip().lower() or "sync")
            if action not in ("sync", "preview", "status"):
                self.update_chat(self.bot_name, "Usage: /sync, /sync preview, /sync status", speak=False)
                return
            if action != "status":
                self._set_busy(True)
                self.update_chat(self.bot_name, f"...running Drive {action} (this can take a few minutes)...", speak=False)

            def finish(r):
                self._set_busy(False)
                self.update_chat(self.bot_name, r)

            self._in_background(lambda: self._run_local_tool("drive_sync", {"action": action}), finish)
        elif cmd in ("remind", "reminder"):
            self.local_reminder(rest)
        elif cmd in ("reminders", "schedule"):
            if rest.strip().startswith(("remove ", "delete ", "cancel ")):
                self._bg_tool("reminder", {"action": "remove", "text": rest.split(" ", 1)[1]})
            else:
                self.update_chat(self.bot_name, self._run_local_tool("reminder", {"action": "list"}), speak=False)
        elif cmd == "decisions":
            def done(r):
                self._set_busy(False)
                n, text = r if isinstance(r, tuple) else (0, r)
                self.update_chat(self.bot_name, text, speak=False)
                self.speak(f"{n} items are waiting on you. They're on screen." if n else "Nothing is waiting on your decision right now.")
            self._set_busy(True)
            self.update_chat(self.bot_name, "...looking for anything waiting on your decision (about 30 seconds)...", speak=False)
            import va_extras
            self._in_background(lambda: va_extras.decisions_digest(self.config), done)
        elif cmd == "claude":
            if not rest.strip():
                self.update_chat(self.bot_name, "Usage: /claude <question or task>", speak=False)
                return
            self._bg_tool("ask_claude", {"question": rest.strip()}, note="...asking Claude (this can take a minute or two)...", speak=True)
        elif cmd == "find":
            self._bg_tool("find_files", {"query": rest.strip()}, note="...searching your drives...")
        elif cmd == "calendar":
            self._bg_tool("calendar", {"days": int(rest.strip() or 1) if rest.strip().isdigit() else 1}, speak=True)
        elif cmd in ("kdp", "books"):
            self._bg_tool("kdp_tracker", {"title": rest.strip() or None}, note="...checking the book projects folder...", speak=True)
        elif cmd == "search":
            self._bg_tool("web_search", {"query": rest.strip()}, note="...searching the web...")
        elif cmd in ("backup-valerie", "backupval"):
            import va_extras
            self._set_busy(True)
            self.update_chat(self.bot_name, "...backing myself up to Google Drive...", speak=False)
            self._in_background(lambda: va_extras.backup_valerie(self.config),
                                lambda r: (self._set_busy(False), self.update_chat(self.bot_name, r)))
        elif cmd == "voice":
            want = rest.strip().lower()
            on = self.config.get("assistant", {}).get("speak_replies", True)
            if (want == "off" and on) or (want == "on" and not on) or not want:
                self.toggle_voice()
        elif cmd in ("checkin", "check-in"):
            self.run_checkin()
        elif cmd == "backup":
            self.update_chat(self.bot_name, "...reading the backup log...", speak=False)
            self._in_background(lambda: self._run_local_tool("backup_status"),
                                lambda r: self.update_chat(self.bot_name, r))
        elif cmd == "mail":
            words = rest.split()
            account = (self.ws_ids() or [None])[0]
            if words and (words[0].lower() in self.ws_ids() or ws.detect(self.config, words[0].lower())):
                account = words[0].lower() if words[0].lower() in self.ws_ids() else ws.detect(self.config, words[0].lower())
                words = words[1:]
            if account is None:
                self.update_chat(self.bot_name, "No mailboxes are set up yet - open Settings > Workspaces.", speak=False)
                return
            arg = " ".join(words)
            if arg.startswith("read "):
                self.read_email(account, None, arg[5:].strip())
                return
            elif arg == "unread":
                tool, args = "mail_list", {"account": account, "unread_only": True, "limit": 15}
            else:
                tool, args = "mail_list", {"account": account, "limit": 10}

            def show(r):
                self.update_chat(self.bot_name, r, speak=False)

            self.update_chat(self.bot_name, f"...checking the {account} inbox...", speak=False)
            self._in_background(lambda: self._run_local_tool(tool, args), show)
        elif cmd == "clear":
            self.histories[self.focus].clear()
            self.update_chat(self.bot_name, f"Conversation cleared ({self.focus} focus).")
        elif cmd == "remember":
            if rest.strip():
                knowledge.add_note(rest)
                self.update_chat(self.bot_name, "Got it - I'll remember that.")
            else:
                self.update_chat(self.bot_name, "Usage: /remember <fact>", speak=False)
        else:
            sk = skills_mod.load_skills().get(cmd)
            if sk:
                self.call_brain(skills_mod.render(sk, rest), shown=f"[{cmd}] {rest}")
            else:
                self.update_chat(self.bot_name, f"Unknown command /{cmd}. Type /help.", speak=False)

    def call_brain(self, prompt, shown=None):
        provider = self.config.get("active_provider", "ollama")
        self._set_busy(True)
        self.update_chat(self.bot_name, f"...thinking ({provider})...", speak=False)

        def threadsafe_update(sender, message, speak=True):
            self.root.after(0, lambda: self.update_chat(sender, message, speak=speak))

        focus = self.focus
        hist = self.histories[focus]

        def work():
            return run_agent_loop(provider, prompt, self.config, update_chat=threadsafe_update,
                                  history=hist, ecosystem=None if focus == "both" else focus)

        def done(response):
            if response == "__CANCELLED__":
                return
            hist.extend([{"role": "user", "content": shown or prompt},
                         {"role": "assistant", "content": response}])
            del hist[:-40]
            cfg_mod.log_activity("chat", f"q={(shown or prompt)[:200]!r} a={response[:300]!r}")
            self._set_busy(False)
            self._refresh_status()
            self.update_chat(self.bot_name, response)

        self._in_background(work, done)

    def _in_background(self, fn, on_done, keep=False):
        """Run fn in a thread and hand its result to on_done on the Tk thread.
        After STOP, results of jobs started before it are thrown away
        (unless keep=True, e.g. email filing that should still report)."""
        self._job_seq += 1
        token = self._job_seq

        def runner():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                result = f"Something went wrong: {e}"

            def deliver():
                if token <= self._cancelled_upto and not keep:
                    return
                on_done(result)
            self.root.after(0, deliver)
        threading.Thread(target=runner, daemon=True).start()

    def _bg_tool(self, name, args, note=None, speak=False):
        def done(r):
            self._set_busy(False)
            self.update_chat(self.bot_name, r, speak=speak)
        self._set_busy(True)
        self.update_chat(self.bot_name, note or "...working on it...", speak=False)
        self._in_background(lambda: self._run_local_tool(name, args), done)

    def local_reminder(self, text):
        """'remind me at 3pm to call the printer' - parsed right here, no AI."""
        import reminders
        body = re.sub(r"^(please )?(remind me|set a reminder|reminder)\s*(to |that |about )?", "", text.strip(), flags=re.I)
        what = re.sub(r"\b(every (week)?day|every weekday|weekdays|daily|tomorrow( morning)?|tonight|this afternoon|"
                      r"(on |next |this |every )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                      r"in (\d+|an?|one|two|three|half an?) ?(minutes?|mins?|hours?|hrs?)|"
                      r"at \d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?|am|pm)?|at noon|at midnight|\d{1,2}:\d{2})\b", "", body, flags=re.I)
        what = re.sub(r"^\s*(to|that|about)\s+", "", re.sub(r"\s{2,}", " ", what)).strip(" ,.") or "Reminder"
        try:
            item = reminders.add(what, body)
            self.update_chat(self.bot_name, f"Okay - I'll remind you {reminders.describe(item)}: {what}.")
        except reminders.ReminderError as e:
            self.update_chat(self.bot_name, str(e))

    def _schedule_tick(self):
        """Every 30 s: fire due reminders / scheduled check-ins and backups."""
        import reminders
        if self.paused:
            self.root.after(30000, self._schedule_tick)
            return
        try:
            due = reminders.pop_due()
        except Exception:
            due = []
        for item in due:
            self._run_scheduled(item)
        self.root.after(30000, self._schedule_tick)

    def _run_scheduled(self, item):
        import reminders
        act = item.get("action", "speak")
        eco = item.get("ecosystem", "none")
        ecos = tuple(self.ws_ids()) if eco not in self.ws_ids() else (eco,)
        if act != "backup" and self.busy:   # retry shortly instead of interrupting
            reminders.add(item["text"], when=datetime.now().replace(second=0, microsecond=0) + __import__("datetime").timedelta(minutes=2),
                          action=act, ecosystem=eco, prompt=item.get("prompt", ""))
            return
        if act == "checkin":
            self.run_checkin(scheduled=True, ecosystems=ecos)
        elif act == "report":
            self.report_ecosystem(ecos[0], then=None, scheduled=True)
        elif act == "decisions":
            import va_extras
            self._set_busy(True)
            self._in_background(lambda: va_extras.decisions_digest(self.config, ecosystems=ecos),
                                lambda r: (self._set_busy(False), self.update_chat(self.bot_name, r[1] if isinstance(r, tuple) else r, speak=False),
                                           self.speak(f"{time_greeting()}{self.owner_suffix()}. {r[0] if isinstance(r, tuple) else 0} items are waiting on you.", force=True)))
        elif act == "backup":
            import va_extras
            self._in_background(lambda: va_extras.backup_valerie(cfg_mod.load_config()),
                                lambda r: self.update_chat(self.bot_name, r, speak=False))
        elif act in ("prompt", "skill"):
            if eco in self.ws_ids():
                self.set_focus(eco)
            text = item.get("prompt", "")
            if act == "skill":
                sk = skills_mod.load_skills().get(item.get("skill", ""))
                if not sk:
                    self.update_chat(self.bot_name, f"Scheduled task '{item['text']}': skill not found.", speak=False)
                    return
                text = skills_mod.render(sk, text)
            self.update_chat(self.bot_name, f"Scheduled: {item['text']}", speak=False)
            self.call_brain(text, shown=f"[scheduled] {item['text']}")
        else:
            msg = item.get("prompt") or item["text"]
            self.update_chat(self.bot_name, f"Reminder: {msg}", speak=False)
            self.speak(f"{time_greeting()}{self.owner_suffix()}. Here's your reminder: {msg}", force=True)
            if shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "-u", "critical", "Valerie reminder", msg])

    CANCEL_WORDS = re.compile(r"^\W*(hey |ok |okay )?(valerie|valery|val)?[\s,.!]*(stop|cancel|never ?mind|forget it|abort|"
                              r"ignore that|scratch that|no,? stop|wait|hold on|not that|that'?s wrong|wrong)\b", re.I)

    def cancel_current(self, event=None, quiet=False):
        """STOP button / Esc / 'Valerie, stop': abandon the running job."""
        if self._pending_talk is not None:
            self._pending_talk = None
            self._voice_state("Cancelled - listening again")
            if not quiet:
                self.update_chat(self.bot_name, "(Cancelled - I didn't send that.)", speak=False)
            return
        was_busy = self.busy
        self._cancelled_upto = self._job_seq
        try:
            import brain
            brain.cancel()
        except Exception:
            pass
        try:
            import va_extras
            va_extras.kill_active()
        except Exception:
            pass
        self.stop_speaking()
        if was_busy:
            self._set_busy(False)
            if not quiet:
                self.update_chat(self.bot_name, "Stopped. What would you like instead?", speak=False)

    def _set_busy(self, busy):
        self.busy = busy
        if busy:
            self.send_btn.config(state=tk.NORMAL, text="STOP", bg="#b23b3b", activebackground="#8f2f2f",
                                 command=self.cancel_current)
        else:
            self.send_btn.config(state=tk.NORMAL, text="SEND", bg=AMBER, activebackground="#9c5f1f",
                                 command=self.on_send)
        if hasattr(self, "voice_stop_btn"):
            if busy and getattr(self, "talk_mode", False):
                self.voice_stop_btn.pack(pady=(4, 0))
            else:
                self.voice_stop_btn.pack_forget()
        if busy:
            self._busy_started = datetime.now()
            self._tick()
        else:
            self._refresh_status()

    def _tick(self):
        if not self.busy:
            return
        secs = int((datetime.now() - self._busy_started).total_seconds())
        self.status.config(text=f"Thinking... {secs // 60}m {secs % 60:02d}s  (local AI on CPU - long questions take a few minutes)")
        if self.talk_mode:
            self._voice_state(f"Thinking... {secs // 60}m {secs % 60:02d}s")
        self.root.after(1000, self._tick)

    # ------------------------------------------------------- Microphone

    def on_mic_press(self, event=None):
        if not self.config.get("speech_input", {}).get("enabled", True):
            return
        missing = listen.missing_pieces()
        if missing:
            self.update_chat(self.bot_name, f"Microphone input needs: pip install {' '.join(missing)} (in Valerie's venv).")
            return
        try:
            self.recorder.start()
            self.is_recording = True
            self.mic_btn.config(text="Listening... (release to stop)", bg="#CC3333")
        except Exception as e:
            self.update_chat(self.bot_name, f"Couldn't start the microphone: {e}")

    def on_mic_release(self, event=None):
        if not self.is_recording:
            return
        self.is_recording = False
        self.mic_btn.config(text="Transcribing...", bg="#999999")
        audio = self.recorder.stop()
        model_size = self.config.get("speech_input", {}).get("model_size", "tiny")

        def _work():
            try:
                text = listen.transcribe(audio, model_size=model_size)
            except Exception as e:  # noqa: BLE001
                text = f"[speech recognition error: {e}]"
            self.root.after(0, lambda: self._on_transcribed(text))

        threading.Thread(target=_work, daemon=True).start()

    def _on_transcribed(self, text):
        self.mic_btn.config(text="Hold to Talk", bg="#444444")
        text = (text or "").strip()
        if not text:
            self.update_chat(self.bot_name, "I didn't catch that - try holding the button a little longer while you talk.")
            return
        if text.startswith("[speech recognition error"):
            self.update_chat(self.bot_name, text, speak=False)
            return
        # Put the words in the input box to check/edit; Enter or SEND sends.
        self.user_input.delete(0, tk.END)
        self.user_input.insert(0, text)
        self.user_input.focus_set()
        self.user_input.icursor(tk.END)

    # ---------------------------------------------------------------- UI

    def setup_ui(self):
        self.root.configure(bg=BG)
        self.root.minsize(460, 600)

        # ---- Header ---------------------------------------------------------
        header = tk.Frame(self.root, bg=HEADER, pady=12)
        header.pack(fill=tk.X, side=tk.TOP)
        try:
            from PIL import Image, ImageTk
            if os.path.exists(self.image_path):
                img = Image.open(self.image_path).resize((64, 64))
                self.photo = ImageTk.PhotoImage(img)
                tk.Label(header, image=self.photo, bg=HEADER).pack(side=tk.LEFT, padx=(16, 8))
        except Exception:
            pass
        titles = tk.Frame(header, bg=HEADER)
        titles.pack(side=tk.LEFT, padx=(16, 0))
        tk.Label(titles, text=self.bot_name, font=(FONT, 18, "bold"), bg=HEADER, fg=GOLD).pack(anchor="w")
        tk.Label(titles, text="Personal assistant  -  Enter sends, Shift+Enter new line, /help", font=(FONT, 9),
                 bg=HEADER, fg="#e8dcc4").pack(anchor="w")
        tk.Button(header, text="Settings", command=self.open_settings, font=(FONT, 9, "bold"),
                  relief=tk.FLAT, bg=GOLD, fg=INK, activebackground="#e8bd66",
                  padx=10, pady=4, cursor="hand2").pack(side=tk.RIGHT, padx=(6, 16))
        self.voice_toggle_btn = tk.Button(header, text="", command=self.toggle_voice, font=(FONT, 9, "bold"),
                                          relief=tk.FLAT, padx=10, pady=4, cursor="hand2")
        self.voice_toggle_btn.pack(side=tk.RIGHT)
        self._paint_voice_toggle()
        self.focus_btn = tk.Button(header, text="", command=self.cycle_focus, font=(FONT, 9, "bold"), fg="white",
                                   relief=tk.FLAT, padx=10, pady=4, cursor="hand2")
        self.focus_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self._paint_focus()

        # ---- Quick actions -------------------------------------------------
        quick = tk.Frame(self.root, bg=PANEL, pady=4)
        quick.pack(fill=tk.X, side=tk.TOP)
        integ = self.config.get("integrations", {})
        buttons = [("Check-in", lambda: self._quick("/checkin")),
                   ("Email", lambda: self._quick("check my email")),
                   ("Sync Drive", lambda: self._quick("/sync")) if integ.get("drive_sync_script")
                   else ("Waiting on me", lambda: self._quick("what needs my decision")),
                   ("Backup status", lambda: self._quick("/backup")) if integ.get("backup_log")
                   else ("Calendar", lambda: self._quick("/calendar 1")),
                   ("System check", lambda: self._quick("system check")),
                   ("Skills", lambda: self._quick("/skills")),
                   ("Pause", self.toggle_pause)]
        for i, (label, action) in enumerate(buttons):
            r, c = divmod(i, 4)
            b = tk.Button(quick, text=label, command=action, font=(FONT, 9), relief=tk.FLAT,
                      bg=BG, fg=INK, activebackground=GOLD, pady=2,
                      cursor="hand2")
            b.grid(row=r, column=c, sticky="ew", padx=4, pady=2)
            if label == "Pause":
                self.pause_btn = b
        for c in range(4):
            quick.grid_columnconfigure(c, weight=1, uniform="quick")

        # ---- Status bar (bottom-most) --------------------------------------
        self.status = tk.Label(self.root, text="", anchor="w", font=(FONT, 8), bg=HEADER, fg="#e8dcc4", padx=10)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        self._refresh_status()

        # ---- Input row -----------------------------------------------------
        bottom = tk.Frame(self.root, bg=BG, pady=10)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.bottom = bottom
        row = tk.Frame(bottom, bg=BG)
        row.pack(fill=tk.X, padx=16)
        self.input_row = row
        self.user_input = ChatInput(row, font=(FONT, 12), relief=tk.FLAT, bd=8, bg="white", fg=INK,
                                   insertbackground=INK, highlightthickness=1,
                                   highlightbackground="#d8cbb3", highlightcolor=AMBER)
        self.user_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.user_input.bind("<Return>", lambda e: (self.on_send(), "break")[1])
        self.user_input.bind("<KP_Enter>", lambda e: (self.on_send(), "break")[1])
        self.user_input.bind("<Shift-Return>", lambda e: None)  # Shift+Enter = new line (default)
        self.user_input.focus_set()
        self.dictate_btn = tk.Button(row, text="MIC", command=self.toggle_dictation, bg=HEADER, fg="white",
                                     activebackground="#3f604c", activeforeground="white",
                                     font=(FONT, 10, "bold"), relief=tk.FLAT, padx=10, cursor="hand2")
        self.dictate_btn.pack(side=tk.LEFT, padx=(8, 0), anchor="s", ipady=6)
        self.send_btn = tk.Button(row, text="SEND", command=self.on_send, bg=AMBER, fg="white",
                                  activebackground="#9c5f1f", activeforeground="white",
                                  font=(FONT, 10, "bold"), relief=tk.FLAT, padx=16, cursor="hand2")
        self.send_btn.pack(side=tk.LEFT, padx=(8, 0), anchor="s", ipady=6)

        self.voice_row = tk.Frame(bottom, bg=BG)
        self.voice_row.pack(fill=tk.X, padx=16, pady=(8, 0))
        self.mic_btn = tk.Button(bottom, text="Hold to Talk", bg=HEADER, fg="white",
                                 activebackground="#3f604c", activeforeground="white",
                                 font=(FONT, 9, "bold"), relief=tk.FLAT, pady=6, cursor="hand2")
        self.mic_btn.pack(side=tk.LEFT, in_=self.voice_row, fill=tk.X, expand=True)
        self.mic_btn.bind("<ButtonPress-1>", self.on_mic_press)
        self.mic_btn.bind("<ButtonRelease-1>", self.on_mic_release)
        self.talk_btn = tk.Button(bottom, text="Talk mode (hands-free)", command=self.toggle_talk_mode,
                                  bg=HEADER, fg="white", activebackground="#3f604c", activeforeground="white",
                                  font=(FONT, 9, "bold"), relief=tk.FLAT, pady=6, cursor="hand2")
        self.talk_btn.pack(side=tk.LEFT, in_=self.voice_row, fill=tk.X, expand=True, padx=(6, 0))

        # ---- Voice panel (Talk mode) -----------------------------------------
        self.voice_panel = tk.Frame(self.root, bg=BG)
        self.voice_status = tk.Label(self.voice_panel, text="", font=(FONT, 16, "bold"), bg=BG, fg=HEADER, pady=18,
                                     wraplength=500, justify="center")
        self.voice_status.pack(fill=tk.X)
        self.voice_cancel_btn = tk.Button(self.voice_panel, text="Cancel - don't send that", command=self.cancel_current,
                                          bg="#b23b3b", fg="white", relief=tk.FLAT, font=(FONT, 10, "bold"), padx=14, pady=4)
        self.voice_stop_btn = tk.Button(self.voice_panel, text="STOP", command=self.cancel_current,
                                        bg="#b23b3b", fg="white", relief=tk.FLAT, font=(FONT, 10, "bold"), padx=24, pady=4)
        tk.Label(self.voice_panel, text="Say \"stop listening\" or \"show chat\" to go back to the chat window.",
                 font=(FONT, 9), bg=BG, fg=MUTED).pack()
        self.voice_you = tk.Label(self.voice_panel, text="", font=(FONT, 11), bg=YOU_BUBBLE, fg=INK,
                                  wraplength=480, justify="left", anchor="w", padx=10, pady=8)
        self.voice_you.pack(fill=tk.X, pady=(18, 6))
        vb_frame = tk.Frame(self.voice_panel, bg=BOT_BUBBLE)
        vb_frame.pack(fill=tk.BOTH, expand=True)
        vb_scroll = tk.Scrollbar(vb_frame, relief=tk.FLAT, troughcolor=BOT_BUBBLE)
        vb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.voice_bot_text = tk.Text(vb_frame, font=(FONT, 11), bg=BOT_BUBBLE, fg=INK, wrap=tk.WORD, relief=tk.FLAT,
                                      padx=10, pady=8, height=8, yscrollcommand=vb_scroll.set, state="disabled",
                                      highlightthickness=0)
        self.voice_bot_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vb_scroll.config(command=self.voice_bot_text.yview)

        class _VoiceBot:   # keeps the old .config(text=...) call working
            def __init__(self, w):
                self.w = w

            def config(self, text=""):
                self.w.config(state="normal")
                self.w.delete("1.0", tk.END)
                self.w.insert("1.0", text)
                self.w.config(state="disabled")
                self.w.yview_moveto(0)
        self.voice_bot = _VoiceBot(self.voice_bot_text)

        # ---- Chat log ------------------------------------------------------
        chat_frame = tk.Frame(self.root, bg=BG)
        chat_frame.pack(padx=16, pady=(10, 0), fill=tk.BOTH, expand=True)
        self.chat_frame = chat_frame
        scroll = tk.Scrollbar(chat_frame, relief=tk.FLAT, troughcolor=BG)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_log = tk.Text(chat_frame, state="disabled", wrap=tk.WORD, bg="white", fg=INK,
                                font=(FONT, 11), relief=tk.FLAT, padx=10, pady=10,
                                highlightthickness=1, highlightbackground="#d8cbb3",
                                yscrollcommand=scroll.set, spacing1=2, spacing3=2)
        self.chat_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=lambda *a: (self.chat_log.yview(*a), self._update_jump_btn()))
        self.jump_btn = tk.Button(chat_frame, text="\u2193 New messages", command=lambda: self.scroll_chat("end"),
                                  bg=HEADER, fg="white", relief=tk.FLAT, font=(FONT, 9, "bold"), padx=10, pady=2,
                                  cursor="hand2", activebackground="#3f604c", activeforeground="white")

        def wheel(event):
            # Scroll the chat (or the Talk-mode reply) from anywhere in the window,
            # including while the cursor is in the text box.
            target = self.voice_bot_text if getattr(self, "talk_mode", False) else self.chat_log
            if event.num == 4 or getattr(event, "delta", 0) > 0:
                target.yview_scroll(-3, "units")
            else:
                target.yview_scroll(3, "units")
            self._update_jump_btn()
            return "break"
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.root.bind_all(seq, lambda e: wheel(e) if str(e.widget).startswith(str(self.root)) and
                               not str(e.widget).startswith(".!toplevel") else None)
        for seq, amount in (("<Prior>", -1), ("<Next>", 1)):
            self.root.bind(seq, lambda e, a=amount: (self.chat_log.yview_scroll(a, "pages"), self._update_jump_btn(), "break")[2])
        self.root.bind("<Control-End>", lambda e: self.scroll_chat("end"))
        self.root.bind("<Control-Home>", lambda e: (self.chat_log.yview_moveto(0), self._update_jump_btn(), "break")[2])
        self.chat_log.tag_config("you_name", foreground=HEADER, font=(FONT, 10, "bold"), justify="right", spacing1=12)
        self.chat_log.tag_config("you", background=YOU_BUBBLE, lmargin1=80, lmargin2=80, rmargin=4,
                                 justify="right", spacing3=1)
        self.chat_log.tag_config("bot_name", foreground=AMBER, font=(FONT, 10, "bold"), spacing1=12)
        self.chat_log.tag_config("bot", background=BOT_BUBBLE, lmargin1=4, lmargin2=4, rmargin=60, spacing3=1)
        self.chat_log.tag_config("note", foreground=MUTED, font=(FONT, 9, "italic"), spacing3=6)
        self.chat_log.tag_config("readlink", foreground=AMBER, font=(FONT, 8, "bold"), spacing3=4)
        self.chat_log.tag_bind("readlink", "<Enter>", lambda e: self.chat_log.config(cursor="hand2"))
        self.chat_log.tag_bind("readlink", "<Leave>", lambda e: self.chat_log.config(cursor=""))



    def set_focus(self, eco, announce=False):
        if eco not in ["both"] + self.ws_ids() or eco == self.focus:
            return
        self.focus = eco
        self._paint_focus()
        self._refresh_status()
        if announce:
            msg = ("Now covering all workspaces." if eco == "both" else
                   f"Now focused on {self.ws_label(eco).upper()} ({ws.tz(self.config, eco).split('/')[-1].replace('_', ' ')} time).")
            self.update_chat(self.bot_name, msg, speak=False)

    def cycle_focus(self):
        order = ["both"] + self.ws_ids()
        cur = order.index(self.focus) if self.focus in order else 0
        self.set_focus(order[(cur + 1) % len(order)], announce=True)

    def _paint_focus(self):
        if hasattr(self, "focus_btn"):
            if len(self.ws_ids()) < 2:
                self.focus_btn.pack_forget()
                return
            if self.focus == "both":
                text, color = "Focus: All", "#8a7a68"
            else:
                text, color = f"Focus: {self.ws_label(self.focus)}", ws.color(self.config, self.focus)
            self.focus_btn.config(text=text, bg=color, activebackground=color)

    def _detect_focus(self, lower):
        """Switch focus when the owner clearly talks about one workspace."""
        hit = ws.detect(self.config, lower)
        if hit:
            self.set_focus(hit, announce=True)

    def toggle_pause(self):
        """Hold everything until the owner is ready: no listening, no scheduled
        tasks (tasks that come due in the meantime run on Resume if they're
        under 2 hours late), and stop whatever is running."""
        self.paused = not self.paused
        if self.paused:
            self.cancel_current(quiet=True)
            self.pause_btn.config(text="Resume", bg=GOLD)
            self.update_chat(self.bot_name, "Paused. I won't listen or run scheduled tasks until you click Resume.", speak=False)
            if self.talk_mode:
                self._voice_state("Paused - click Resume when you're ready")
        else:
            self.pause_btn.config(text="Pause", bg=BG)
            self.update_chat(self.bot_name, "Resumed.", speak=False)
            if self.talk_mode:
                self._voice_state("Listening...")
        self._refresh_status()

    def toggle_voice(self):
        a = self.config.setdefault("assistant", {})
        a["speak_replies"] = not a.get("speak_replies", True)
        if not a["speak_replies"]:
            self.stop_speaking()
        try:
            disk = cfg_mod.load_config()
            disk.setdefault("assistant", {})["speak_replies"] = a["speak_replies"]
            cfg_mod.save_config(disk)
        except Exception:
            pass
        self._paint_voice_toggle()

    def _paint_voice_toggle(self):
        on = self.config.get("assistant", {}).get("speak_replies", True)
        self.voice_toggle_btn.config(text="Voice: ON" if on else "Voice: OFF",
                                     bg="#6b8f5e" if on else "#8a7a68", fg="white",
                                     activebackground="#7fa571" if on else "#9c8b78")

    def _quick(self, text):
        if self.busy:
            return
        self.user_input.delete(0, tk.END)
        self.user_input.insert(0, text)
        self.on_send()

    def _refresh_status(self):
        o = self.config.get("ollama", {})
        provider = self.config.get("active_provider", "ollama")
        model = o.get("model") if provider == "ollama" else self.config.get(provider, {}).get("model", "")
        try:
            st = knowledge.stats()
            kb = f"{st['files']} docs"
        except Exception:
            kb = "index not built"
        n_sk = len(skills_mod.load_skills())
        self.status.config(text=f"{'PAUSED    ' if getattr(self, 'paused', False) else ''}Focus: {getattr(self, 'focus', 'both').title()}    Brain: {provider} / {model}    "
                                f"Knowledge: {kb}    Skills: {n_sk}")

    def _read_aloud_click(self, message):
        """Clicking '> Read aloud' reads that message even when Voice is OFF;
        clicking while she's talking stops her."""
        if self.speech_proc and self.speech_proc.poll() is None:
            self.stop_speaking()
            return
        self.speak(message, force=True)

    def _at_bottom(self):
        try:
            return self.chat_log.yview()[1] >= 0.995
        except tk.TclError:
            return True

    def scroll_chat(self, what):
        if what == "end":
            self.chat_log.see(tk.END)
        else:
            self.chat_log.yview_scroll(what, "units" if abs(what) < 5 else "pages" if False else "units")
        self._update_jump_btn()
        return "break"

    def _update_jump_btn(self, *_):
        if not hasattr(self, "jump_btn"):
            return
        if self._at_bottom():
            self.jump_btn.place_forget()
        else:
            self.jump_btn.place(relx=0.5, rely=1.0, y=-8, anchor="s")

    def update_chat(self, sender, message, speak=True, voice=None):
        stick = self._at_bottom()
        self.chat_log.config(state="normal")
        is_bot = sender == self.bot_name
        if is_bot and message.startswith(("...", "(using")):
            self.chat_log.insert(tk.END, f"{message}\n", "note")
        elif is_bot:
            self.chat_log.insert(tk.END, f"{sender}\n", "bot_name")
            self.chat_log.insert(tk.END, f"{message}\n", "bot")
            self._msg_count = getattr(self, "_msg_count", 0) + 1
            tag = f"read{self._msg_count}"
            self.chat_log.insert(tk.END, "\u25b6 Read aloud\n", ("readlink", tag))
            self.chat_log.tag_bind(tag, "<Button-1>", lambda e, m=(voice or message): self._read_aloud_click(m))
        else:
            self.chat_log.insert(tk.END, f"{sender}\n", "you_name")
            self.chat_log.insert(tk.END, f"{message}\n", "you")
        self.chat_log.config(state="disabled")
        if stick or sender == "You":
            self.chat_log.see(tk.END)     # follow along only if already at the bottom
        self._update_jump_btn()
        if is_bot and not message.startswith(("...", "(using")):
            self._last_talk_reply = __import__("time").time()
        if is_bot and getattr(self, "talk_mode", False) and not message.startswith(("...", "(using")):
            self.voice_bot.config(text=f"Valerie: {message}")
            self._voice_state("Speaking...")
        if is_bot and speak:
            self.speak(message)

    # ----------------------------------------------------------- Settings

    def open_settings(self):
        """Sidebar-style Settings (settings_ui.py): General, Voice & Mic, Mail,
        Skills, Connectors, Tools, Schedule, Knowledge, Calendar & Cloud."""
        import settings_ui
        existing = getattr(self, "_settings_win", None)
        if existing is not None and existing.win.winfo_exists():
            existing.win.lift()
            return
        self._settings_win = settings_ui.SettingsWindow(self)


if __name__ == "__main__":
    root = tk.Tk()
    app = ValerieAssistantGUI(root)
    root.mainloop()
