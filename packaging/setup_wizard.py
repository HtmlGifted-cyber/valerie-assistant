#!/usr/bin/env python3
"""
Valerie first-run setup (runs with the system python3 + tkinter).

Everything is installed into the user's own folders - nothing runs as root:
  1. Python environment  ~/.local/share/valerie-assistant/venv
  2. Voice engine (Piper) + a voice   ~/.local/share/valerie-assistant/{piper,voices}
  3. Local AI: checks for Ollama; if missing, opens a terminal with the
     official installer for the user to approve; then downloads the model.
  4. About you: name and workspaces.

Re-running is safe: finished steps are skipped.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tkinter as tk
import urllib.request
from tkinter import messagebox

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(APP_ROOT, "app"))
import config as cfg_mod  # noqa: E402  (stdlib-only module)
import workspaces as W    # noqa: E402

DATA = cfg_mod.DATA_DIR
VENV = os.path.join(DATA, "venv")
PY = os.path.join(VENV, "bin", "python")
PIPER_DIR = cfg_mod.USER_PIPER_DIR
VOICES = cfg_mod.USER_VOICES_DIR
MODEL = "qwen2.5:3b"
VOICE_ID = "en_US-amy-medium"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium"
PIPER_URL = "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_{arch}.tar.gz"
OLLAMA_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"

BG, PANEL, HEADER, GOLD, AMBER, INK, MUTED = "#f6f1e7", "#efe6d4", "#2f4a3a", "#d9a441", "#b8742a", "#3b2f25", "#7a6a58"
FONT = "DejaVu Sans"
TZS = ["America/New_York", "America/Detroit", "America/Chicago", "America/Denver", "America/Phoenix",
       "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Vancouver",
       "Europe/London", "Europe/Paris", "Europe/Berlin", "Australia/Sydney", "Asia/Tokyo", "UTC"]


def local_tz():
    try:
        with open("/etc/timezone") as f:
            return f.read().strip()
    except OSError:
        return "UTC"


class Wizard:
    def __init__(self):
        self.ok = False
        self.root = tk.Tk()
        self.root.title("Set up Valerie")
        self.root.geometry("720x560")
        self.root.configure(bg=BG)
        head = tk.Frame(self.root, bg=HEADER, pady=12); head.pack(fill=tk.X)
        tk.Label(head, text="Valerie", fg=GOLD, bg=HEADER, font=(FONT, 18, "bold")).pack(anchor="w", padx=18)
        self.sub = tk.Label(head, text="", fg="#e8dcc4", bg=HEADER, font=(FONT, 10)); self.sub.pack(anchor="w", padx=18)
        self.body = tk.Frame(self.root, bg=BG, padx=22, pady=14); self.body.pack(fill=tk.BOTH, expand=True)
        bar = tk.Frame(self.root, bg=PANEL, pady=8); bar.pack(fill=tk.X, side=tk.BOTTOM)
        self.next_btn = tk.Button(bar, text="Next", bg=AMBER, fg="white", relief=tk.FLAT, padx=18, pady=3,
                                  font=(FONT, 10, "bold"), command=lambda: self.next_cb())
        self.next_btn.pack(side=tk.RIGHT, padx=16)
        tk.Button(bar, text="Quit", relief=tk.FLAT, bg=PANEL, command=self.quit).pack(side=tk.RIGHT)
        self.next_cb = lambda: None
        self.page_welcome()

    # ------------------------------------------------------------ helpers
    def clear(self, subtitle):
        for w in self.body.winfo_children():
            w.destroy()
        self.sub.config(text=subtitle)

    def text(self, t, size=10, bold=False, fg=INK, pady=(0, 6)):
        tk.Label(self.body, text=t, bg=BG, fg=fg, font=(FONT, size, "bold" if bold else "normal"),
                 justify="left", anchor="w", wraplength=660).pack(anchor="w", pady=pady)

    def quit(self):
        if messagebox.askyesno("Quit setup?", "Quit setup? You can finish it next time you open Valerie."):
            self.root.destroy()

    # ------------------------------------------------------------- pages
    def page_welcome(self):
        self.clear("Welcome - let's get you set up (about 10-20 minutes, mostly downloading)")
        self.text("Valerie is a personal assistant that runs on this computer.", 12, True)
        self.text("Setup will download into your own home folder (nothing is installed system-wide):")
        for line in ["- Valerie's Python components (about 600 MB)",
                     "- A natural-sounding voice (about 70 MB)",
                     "- The local AI model, if you don't have it yet (about 2 GB) - via Ollama",
                     "Then you'll tell Valerie your name and set up your workspaces."]:
            self.text(line, pady=(0, 2))
        self.text("You can quit at any time; finished steps are kept.", 9, fg=MUTED, pady=(14, 0))
        self.next_cb = self.page_install
        self.next_btn.config(text="Start", state=tk.NORMAL)

    def page_install(self):
        self.clear("Step 1 of 3 - installing components")
        self.status = tk.Label(self.body, text="", bg=BG, fg=HEADER, font=(FONT, 11, "bold"), anchor="w")
        self.status.pack(fill=tk.X)
        frame = tk.Frame(self.body, bg=BG); frame.pack(fill=tk.BOTH, expand=True, pady=6)
        sb = tk.Scrollbar(frame); sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log = tk.Text(frame, height=18, bg="white", font=("DejaVu Sans Mono", 8), yscrollcommand=sb.set)
        self.log.pack(fill=tk.BOTH, expand=True); sb.config(command=self.log.yview)
        self.next_btn.config(text="Please wait...", state=tk.DISABLED)
        threading.Thread(target=self.install_steps, daemon=True).start()

    def say(self, line=None, status=None):
        def ui():
            if status:
                self.status.config(text=status)
            if line:
                self.log.insert(tk.END, line.rstrip() + "\n")
                self.log.see(tk.END)
        self.root.after(0, ui)

    def run(self, cmd, env=None):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        for line in p.stdout:
            self.say(line)
        return p.wait()

    def install_steps(self):
        try:
            os.makedirs(DATA, exist_ok=True)
            # 1. venv + Python packages
            if not os.path.exists(PY):
                self.say(status="Creating Python environment...")
                if self.run([sys.executable, "-m", "venv", "--system-site-packages", VENV]) != 0:
                    raise RuntimeError("Couldn't create the Python environment (is python3-venv installed?).")
            env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
            self.say(status="Updating the Python package installer...")
            self.run([PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip", "wheel"], env)
            req = os.path.join(APP_ROOT, "requirements.txt")
            self.say(status="Installing Python components (this is the longest step)...")
            failed = []
            with open(req) as f:
                for raw in f:
                    pkg = raw.split("#")[0].strip()
                    if not pkg:
                        continue
                    self.say(f"--- {pkg}")
                    if self.run([PY, "-m", "pip", "install", "--quiet", "--prefer-binary", pkg], env) != 0:
                        failed.append(pkg)
            if failed:
                self.say(f"Some optional components didn't install: {', '.join(failed)}")
            # 2. Piper voice engine
            if not os.path.exists(os.path.join(PIPER_DIR, "piper")):
                arch = {"x86_64": "x86_64", "aarch64": "aarch64", "armv7l": "armv7l"}.get(platform.machine(), "x86_64")
                self.say(status="Downloading the voice engine (Piper)...")
                tmp = tempfile.mktemp(suffix=".tar.gz")
                urllib.request.urlretrieve(PIPER_URL.format(arch=arch), tmp)
                with tarfile.open(tmp) as t:
                    for m in t.getmembers():  # extract only inside the data folder
                        if m.name.startswith("/") or ".." in m.name:
                            continue
                        t.extract(m, DATA)
                os.remove(tmp)
            # 3. Voice
            os.makedirs(VOICES, exist_ok=True)
            for ext in (".onnx", ".onnx.json"):
                dest = os.path.join(VOICES, VOICE_ID + ext)
                if not os.path.exists(dest):
                    self.say(status="Downloading a voice...")
                    urllib.request.urlretrieve(VOICE_URL + ext, dest + ".part")
                    os.replace(dest + ".part", dest)
            self.say("Components ready.", "Components ready.")
            self.root.after(0, self.page_ai)
        except Exception as e:  # noqa: BLE001
            self.say(f"ERROR: {e}", "Something went wrong - see below. Check your internet connection and press Retry.")
            self.root.after(0, lambda: (self.next_btn.config(text="Retry", state=tk.NORMAL),
                                        setattr(self, "next_cb", self.page_install)))

    # ------------------------------------------------------------ AI step
    def ollama_ok(self):
        if not shutil.which("ollama"):
            return False, False
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.load(r).get("models", [])]
            return True, MODEL in names
        except Exception:
            return True, None

    def page_ai(self):
        self.clear("Step 2 of 3 - the local AI brain")
        have, model = self.ollama_ok()
        if not have:
            self.text("Valerie's AI runs locally with Ollama, which isn't installed yet.", 11, True)
            self.text("Click the button below. A terminal opens with Ollama's official installer; it will ask for "
                      "your password. When it finishes, come back and click Check again.")
            self.text(f"Installer command:  {OLLAMA_INSTALL}", 9, fg=MUTED)
            tk.Button(self.body, text="Open installer in a terminal", bg=HEADER, fg="white", relief=tk.FLAT, padx=12,
                      command=self.open_ollama_installer).pack(anchor="w", pady=8)
            self.text("Prefer a cloud AI instead (Claude, ChatGPT or Gemini with your own API key)? Click Skip; "
                      "you can add the key in Settings later.", 9, fg=MUTED)
            self.next_btn.config(text="Check again", state=tk.NORMAL)
            self.next_cb = self.page_ai
            tk.Button(self.body, text="Skip for now", relief=tk.FLAT, bg=PANEL, command=self.page_you).pack(anchor="w", pady=6)
            return
        if model is None:
            self.text("Ollama is installed but not running. Starting it...", 11, True)
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.root.after(4000, self.page_ai)
            return
        if model:
            self.text(f"The AI model ({MODEL}) is ready.", 11, True)
            self.next_btn.config(text="Next", state=tk.NORMAL)
            self.next_cb = self.page_you
            return
        self.text(f"Downloading the AI model ({MODEL}, about 2 GB)...", 11, True)
        self.pull_status = tk.Label(self.body, text="", bg=BG, fg=MUTED, font=(FONT, 9)); self.pull_status.pack(anchor="w")
        self.next_btn.config(text="Please wait...", state=tk.DISABLED)
        threading.Thread(target=self.pull_model, daemon=True).start()

    def open_ollama_installer(self):
        cmd = f"{OLLAMA_INSTALL}; echo; echo 'Done - you can close this window and click Check again.'; read -r _"
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["xfce4-terminal", "-x"], ["konsole", "-e"], ["xterm", "-e"]):
            if shutil.which(term[0]):
                subprocess.Popen(term + ["bash", "-c", cmd])
                return
        messagebox.showinfo("Install Ollama", f"Open a terminal and run:\n\n{OLLAMA_INSTALL}")

    def pull_model(self):
        p = subprocess.Popen(["ollama", "pull", MODEL], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        buf = ""
        while True:
            ch = p.stdout.read(1)
            if not ch:
                break
            if ch in "\r\n":
                if buf.strip():
                    line = buf.strip()[-90:]
                    self.root.after(0, lambda l=line: self.pull_status.config(text=l))
                buf = ""
            else:
                buf += ch
        ok = p.wait() == 0
        self.root.after(0, lambda: self.page_ai() if ok else (
            self.pull_status.config(text="The download didn't finish - check your connection and click Retry."),
            self.next_btn.config(text="Retry", state=tk.NORMAL), setattr(self, "next_cb", self.page_ai)))

    # ------------------------------------------------------------ About you
    def page_you(self):
        self.clear("Step 3 of 3 - about you")
        cfg = cfg_mod.load_config()
        self.text("What should Valerie call you?", 11, True)
        self.name = tk.Entry(self.body, width=30, font=(FONT, 11)); self.name.insert(0, cfg.get("owner", "")); self.name.pack(anchor="w")
        self.text("Workspaces", 11, True, pady=(16, 2))
        self.text("Separate areas Valerie keeps apart - e.g. 'Work' and 'Home', or two businesses. Each gets its own "
                  "mailbox, memory, knowledge and time zone. You can add mail details later in Settings > Workspaces.",
                  9, fg=MUTED)
        grid = tk.Frame(self.body, bg=BG); grid.pack(anchor="w")
        for c, h in enumerate(["Name", "Time zone", "Email address (optional)"]):
            tk.Label(grid, text=h, bg=BG, fg=MUTED, font=(FONT, 8)).grid(row=0, column=c, sticky="w")
        self.rows = []
        existing = list(cfg.get("workspaces", {}).values())
        for i in range(3):
            e = existing[i] if i < len(existing) else {}
            n = tk.Entry(grid, width=18); n.insert(0, e.get("label", "Work" if i == 0 else ""))
            tzv = tk.StringVar(value=e.get("tz") or local_tz())
            om = tk.OptionMenu(grid, tzv, *([local_tz()] + [t for t in TZS if t != local_tz()]))
            m = tk.Entry(grid, width=30); m.insert(0, e.get("mail", {}).get("address", ""))
            n.grid(row=i + 1, column=0, padx=(0, 6), pady=2); om.grid(row=i + 1, column=1, sticky="w"); m.grid(row=i + 1, column=2, padx=6)
            self.rows.append((n, tzv, m))
        self.next_btn.config(text="Finish", state=tk.NORMAL)
        self.next_cb = self.finish

    def finish(self):
        cfg = cfg_mod.load_config()
        cfg["owner"] = self.name.get().strip()
        wss = {}
        for i, (n, tzv, m) in enumerate(self.rows):
            label = n.get().strip()
            if not label:
                continue
            wid = W.slug(label)
            while wid in wss:
                wid += "2"
            old = cfg.get("workspaces", {}).get(wid, {})
            w = W.new_workspace(label, tzv.get(), color=W.DEFAULT_COLORS[i % len(W.DEFAULT_COLORS)])
            w.update({k: v for k, v in old.items() if k not in ("label", "tz")})
            w["mail"]["address"] = m.get().strip() or w["mail"].get("address", "")
            wss[wid] = w
        cfg["workspaces"] = wss
        cfg["setup_done"] = True
        cfg_mod.save_config(cfg)
        cfg_mod.seed_user_data()
        self.ok = True
        self.root.destroy()


if __name__ == "__main__":
    wiz = Wizard()
    wiz.root.mainloop()
    sys.exit(0 if wiz.ok else 1)
