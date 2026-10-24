"""
config.py - Valerie Local Assistant configuration store.

Holds API keys, the active AI provider, the Google web app connector
settings, knowledge-base folders, and the list of locally scheduled
tasks. Lives under the user's own home directory, so a normal user can
edit it without sudo and it survives package upgrades.

2.2 (user-space build): the app itself now also lives in the user's
home (~/.local/share/valerie-assistant/app) so it can be upgraded
without root; the original /usr/share install is left untouched and
still supplies the Piper binary and the bundled voice.

Nothing in this file talks to the network. It only reads/writes JSON.
"""

import json
import os
import stat

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "valerie-assistant")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_DIR = os.path.join(HOME, ".local", "share", "valerie-assistant")
ACTIVITY_LOG_PATH = os.path.join(LOG_DIR, "activity.log")

# User-space data folders (all writable without sudo).
DATA_DIR = LOG_DIR
APP_DIR = os.path.join(DATA_DIR, "app")
KNOWLEDGE_DIR = os.path.join(DATA_DIR, "knowledge")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
USER_VOICES_DIR = os.path.join(DATA_DIR, "voices")
KB_INDEX_PATH = os.path.join(DATA_DIR, "kb_index.json")
NOTES_PATH = os.path.join(KNOWLEDGE_DIR, "notes.md")

# Where the packaged app ships its defaults (skills, knowledge, example
# connectors) and where older installs kept Piper + the bundled voice.
APP_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_DIR = os.path.join(os.path.dirname(APP_CODE_DIR), "defaults")
SYSTEM_DIR = "/usr/share/valerie-assistant"          # legacy 2.x install (optional)
USER_PIPER_DIR = os.path.join(DATA_DIR, "piper")

DEFAULT_CONFIG = {
    # Set by the first-run wizard.
    "setup_done": False,
    "owner": "",

    # One of: "ollama", "omniroute", "anthropic", "openai", "gemini", "perplexity".
    "active_provider": "ollama",

    # qwen2.5:3b - tool-calling capable and small enough for an ordinary
    # laptop (CPU only). Faster: qwen2.5:1.5b. Smarter (needs ~6 GB free
    # RAM): qwen2.5:7b or llama3.1:8b.
    "ollama": {
        "host": "http://127.0.0.1:11434",
        "model": "qwen2.5:3b",
        "num_ctx": 6144,
        "timeout": 900,
        "keep_alive": "2h",
    },

    "anthropic": {"api_key": "", "model": "claude-opus-5-5"},
    "openai": {"api_key": "", "model": "gpt-4o"},
    "gemini": {"api_key": "", "model": "gemini-2.0-flash"},
    # Perplexity Agent API: web-grounded answers with citations (see brain.py,
    # _run_perplexity). No "api_key" here on purpose - the key is read only
    # from the PERPLEXITY_API_KEY environment variable and never stored in
    # config.json. "preset" bundles model + tools + limits (fast | low |
    # medium | high | xhigh); "model" ("provider/name", e.g.
    # "openai/gpt-5.6-sol") overrides the preset and runs with web_search.
    "perplexity": {"preset": "low", "model": "",
                   # Sharing Valerie's abilities with the Perplexity agent - all opt-in
                   # (see docs/PERPLEXITY.md). skills: names from her skills folder sent
                   # as inline Agent API skills (text instructions only). connectors:
                   # Perplexity Project connector IDs, e.g.
                   #   [{"id": "connector_github", "label": "github"}] - set up once at
                   #   console.perplexity.ai so no credentials live here. mcp: names from
                   #   mcp_servers to forward (cloud HTTPS only; their auth headers are
                   #   sent to Perplexity). Connector/MCP calls ALWAYS ask first.
                   # max_steps: 0 = automatic (12 when skills or shared tools are used).
                   "skills": [], "connectors": [], "mcp": [], "max_steps": 0},
    # OmniRoute: a local router (https://github.com/diegosouzapw/OmniRoute) in front of free/own AI
    # providers with automatic fallback. "model" can be one model ("gemini/gemini-3.8-flash") or a
    # combo name you built in OmniRoute (e.g. "free-chain"). If OmniRoute is down, Valerie falls back to Ollama.
    # How Valerie reaches the owner when away: "ntfy" (free phone push, see va_notify.py setup), "desktop" or "none".
    "notify": {"method": "desktop", "ntfy_server": "https://ntfy.sh"},
    "omniroute": {"base_url": "http://127.0.0.1:20128/v1", "key_file": "~/.omniroute/client.key",
                  "model": "free-chain", "timeout": 240},

    "assistant": {
        "name": "Valerie",
        # {owner} is replaced with the owner's name; the workspace list is
        # appended automatically.
        "persona": (
            "You are Valerie, {owner}'s personal assistant on this Linux computer. "
            "Be warm, practical and concise; lead with what needs a decision. "
            "Use the knowledge snippets you are given when they are relevant, and say when you "
            "don't know something instead of guessing. Use tools when they help; never invent tool "
            "results; anything that sends, deletes or changes something is shown for approval first."
        ),
        "history_turns": 6,
        "pinned_files": [],
        "speak_replies": True,
        "talk_mode_on_start": False,
    },

    # Separate areas Valerie keeps apart (filled in by the first-run wizard).
    # See workspaces.py for the fields.
    "workspaces": {},
    "focus": "both",

    "knowledge": {
        "dirs": [KNOWLEDGE_DIR],
        "max_file_kb": 2048,
        "top_k": 3,
        "max_context_chars": 2200,
    },

    "files": {
        "read_dirs": [KNOWLEDGE_DIR, os.path.join(HOME, "Documents")],
        # Where "find my file" looks. Removable drives under /media/<user> are added automatically.
        "search_roots": [HOME],
    },

    # Optional integrations - each tool stays off until it's configured.
    "integrations": {
        "drive_sync_script": "",     # e.g. ~/bin/drive-sync.sh (status|preview|sync)
        "backup_log": "",            # rclone log to report on, e.g. ~/GoogleDriveBackup.log
        "rclone_backup_remote": "",  # e.g. gdrive:  -> backs Valerie up to "<remote>Valerie Backup"
        "book_series_dir": "",       # folder of book projects for the KDP tracker
    },

    "voice": {"model": "en_US-amy-medium", "length_scale": 1.0, "noise_scale": 0.667, "noise_w": 0.8},
    "speech_input": {"enabled": True, "model_size": "tiny"},
    "talk": {"wake_word": True, "follow_up_seconds": 60},
    "calendar": {"ics_urls": []},
    "web_app": {"url": "", "token": ""},
    "library": {"extra_zim_dir": "/media/wwm/16D4253ED425218B/Offline-Library/"},

    # "auto" = server when that workspace's mailbox has a password, else Thunderbird.
    "mail_source": "auto",
    # After reporting on email the owner asked about: "trash" | "read" | "off".
    "mail_after_report": "read",

    "enabled_tools": {
        "system_check": True, "open_calendar": True, "current_datetime": True, "search_knowledge": True,
        "remember": True, "kdp_cover_calc": False, "backup_status": False, "disk_usage": True,
        "drive_sync": False, "list_files": True, "read_file": True,
        "mail_list": True, "mail_read": True, "mail_mark": True, "mail_delete": True, "mail_send": True,
        "skill_manage": True, "kb_manage": True, "use_connector": True, "reminder": True,
        "decisions": True, "ask_claude": True, "find_files": True, "trash_files": True,
        "calendar": True, "kdp_tracker": False, "web_search": True, "web_read": True,
        # Pay-per-use Perplexity answers; enable in Settings once the key is exported.
        "web_answer": False,
        "sheet_read": False, "sheet_write": False, "sheet_append": False,
        "doc_read": False, "doc_write": False, "doc_create": False,
    },

    # Optional "edge AI worker" on another device on the home network (see va_worker.py). Real values go only
    # in your private config.json, never in the repo.
    "edge_worker": {"enabled": False, "url": "", "token_file": "~/.config/valerie-assistant/edge_worker.token",
                    "timeout": 20},

    # Extra MCP servers (see va_mcp.py and docs/mcp-servers.md). Empty by default.
    "mcp_servers": [],

    # What the network tools watch. Fill in your own devices in your private config.json.
    #   services: [{"name": "...", "host": "<address>", "port": 80, "enabled": true}]
    #   baseline_dir: where the "normal" baselines are kept; status_file: optional JSON from a home dashboard.
    "network_watch": {
        "baseline_dir": "~/.local/share/valerie-assistant/guardian",
        "status_file": "",
        "services": [
            {"name": "home dashboard", "host": "", "port": 80, "enabled": False},
            {"name": "edge worker", "host": "", "port": 0, "enabled": False},
            {"name": "phone web server", "host": "", "port": 0, "enabled": False},
        ],
    },

    "connector_secrets": {},
    "scheduled_tasks": [],
}


def _ensure_dirs():
    for d in (CONFIG_DIR, LOG_DIR, KNOWLEDGE_DIR, SKILLS_DIR, USER_VOICES_DIR):
        os.makedirs(d, exist_ok=True)


def load_config():
    """Load config.json, creating it with safe defaults on first run."""
    _ensure_dirs()
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))

    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_CONFIG))

    # Backfill any keys added by a newer version without clobbering
    # the user's existing values.
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    _deep_merge(merged, cfg)
    import workspaces
    if workspaces.migrate(merged):
        save_config(merged)
    return merged


def seed_user_data():
    """First run: copy the packaged default skills / knowledge notes /
    example connectors into the user's own folders (never overwrites)."""
    import shutil
    _ensure_dirs()
    for sub, dest in (("skills", SKILLS_DIR), ("knowledge", KNOWLEDGE_DIR),
                      ("connectors", os.path.join(DATA_DIR, "connectors"))):
        src = os.path.join(DEFAULTS_DIR, sub)
        if not os.path.isdir(src):
            continue
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(src):
            target = os.path.join(dest, name)
            if not os.path.exists(target):
                shutil.copy2(os.path.join(src, name), target)
    try:   # built-in "mandatory" parts are restored every start (see va_skillsmp.py)
        import va_skillsmp
        va_skillsmp.ensure_mandatory()
    except Exception:
        pass


def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save_config(cfg):
    _ensure_dirs()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    # Config holds API keys - keep it readable only by the owner.
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_api_key(cfg, provider):
    """Prefer an explicit config value, fall back to the environment."""
    env_names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GOOGLE_API_KEY",
    }
    key = cfg.get(provider, {}).get("api_key") or ""
    if key:
        return key
    return os.environ.get(env_names.get(provider, ""), "")


def log_activity(event, detail=""):
    _ensure_dirs()
    import datetime
    line = f"{datetime.datetime.now().isoformat()}\t{event}\t{detail}\n"
    with open(ACTIVITY_LOG_PATH, "a") as f:
        f.write(line)
