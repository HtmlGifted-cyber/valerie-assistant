"""va_specialists.py - Expert "specialist" profiles that Valerie can hand a task to.

A specialist is a Markdown agent file: a `---` frontmatter block (name, description,
optional color/emoji/vibe) followed by a long body that is the specialist's system prompt.
Works with the agency-agents pack (https://github.com/msitarzewski/agency-agents) and
with Claude Code agent files. Nothing is bundled here; point config at the folders you want.

list_specialists(query="", limit=12)     -> ranked matches for a topic, or departments with counts
run_specialist(name_or_query, task, cfg) -> one answer from the specialist, using the owner's own brain
                                            (OmniRoute or Ollama). No tools, no actions taken.

Config: "specialists": {"folders": [...], "max_prompt_chars": 9000}
If "folders" is empty, ~/.claude/agents and ./.claude/agents are used when they exist.

CLI: python va_specialists.py list [query] | run "<name or topic>" "<task>"
"""
import glob
import os
import re
import sys

import requests

import config as cfg_mod

TIMEOUT = 300                  # seconds per model call
DEFAULT_MAX_PROMPT = 9000
NO_PACKS = ("No specialist profiles found. Set specialists.folders in config.json to a folder of "
            "Markdown agent files (for example the agency-agents department folders), or copy "
            ".md agent files into ~/.claude/agents.")
SUFFIX = ("\n\nYou are helping the user of a personal assistant called Valerie. "
          "Answer directly and practically. Do not claim to have taken actions you cannot take.")

_cache = {"sig": None, "items": []}


def _folders(cfg):
    sp = cfg.get("specialists", {}) or {}
    folders = sp.get("folders") or []
    if isinstance(folders, str):
        folders = [folders]
    folders = [os.path.abspath(os.path.expanduser(f)) for f in folders if f]
    if not folders:
        folders = [os.path.expanduser("~/.claude/agents"), os.path.abspath(".claude/agents")]
    return list(dict.fromkeys(f for f in folders if os.path.isdir(f)))


def _read(path):
    """Return (frontmatter dict, body text). Simple line parser, no PyYAML."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read().lstrip("﻿")
    lines = text.splitlines()
    meta = {}
    if not lines or lines[0].strip() != "---":
        return meta, text.strip()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return meta, text.strip()
    for line in lines[1:end]:
        if ":" not in line or line[:1] in (" ", "\t") or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key.strip():
            meta[key.strip().lower()] = val
    return meta, "\n".join(lines[end + 1:]).strip()


def _scan(folders):
    items = []
    for folder in folders:
        for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
            try:
                meta, _ = _read(path)
                mtime = os.path.getmtime(path)
            except (OSError, UnicodeDecodeError):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            items.append({
                "name": meta.get("name") or stem,
                "description": meta.get("description", ""),
                "file": path,
                "mtime": mtime,
                "stem": stem,
                "dept": stem.split("-")[0].lower(),
            })
    return items


def _index(cfg):
    """Cached index; rebuilt when any specialist folder's mtime changes."""
    folders = _folders(cfg)
    sig = tuple((f, os.path.getmtime(f)) for f in folders)
    if sig != _cache["sig"]:
        _cache["items"] = _scan(folders)
        _cache["sig"] = sig
    return _cache["items"]


def _stem_word(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _words(text):
    return {_stem_word(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _rank(items, query):
    qwords = _words(query)
    scored = []
    for it in items:
        nwords = _words(it["name"] + " " + it["stem"])
        dwords = _words(it["description"])
        score = 0
        for w in qwords:
            if w in nwords:
                score += 3          # name match bonus
            elif w in dwords:
                score += 1
        if score:
            scored.append((-score, it["name"].lower(), it))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in scored]


def _resolve(items, name):
    key = (name or "").strip().lower()
    if not key:
        return None
    for it in items:
        if it["name"].lower() == key:
            return it
    for it in items:
        if it["stem"].lower() == key:
            return it
    hits = _rank(items, name)
    return hits[0] if hits else None


def _short(text, n=140):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def list_specialists(query="", limit=12):
    cfg = cfg_mod.load_config()
    items = _index(cfg)
    if not items:
        return NO_PACKS
    query = (query or "").strip()
    if query:
        hits = _rank(items, query)[:max(1, int(limit))]
        if not hits:
            return (f"No specialist matches '{query}' (of {len(items)} profiles). Try a broader topic, "
                    "or list with no query to see the departments.")
        lines = [f"Specialists matching '{query}':"]
        lines += [f"- {it['name']}: {_short(it['description'])}" for it in hits]
        lines.append("Hand a job to one with use_specialist, using the name above.")
        return "\n".join(lines)
    counts = {}
    for it in items:
        counts[it["dept"]] = counts.get(it["dept"], 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = [f"{len(items)} specialist profiles in {len(rows)} departments:"]
    lines += [f"- {dept}: {n}" for dept, n in rows]
    lines.append("Search by topic with: list <topic>")
    return "\n".join(lines)


def _ask_omniroute(messages, cfg):
    o = cfg.get("omniroute", {}) or {}
    base = o.get("base_url", "http://127.0.0.1:20128/v1").rstrip("/")
    key_file = os.path.expanduser(o.get("key_file", "~/.omniroute/client.key"))
    with open(key_file) as f:
        key = f.read().strip()
    body = {"model": o.get("model", "free-chain"), "messages": messages}
    r = requests.post(base + "/chat/completions", json=body,
                      headers={"Authorization": "Bearer " + key}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"OmniRoute HTTP {r.status_code}")
    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("OmniRoute returned an empty answer")
    return text


def _ask_ollama(messages, cfg):
    o = cfg.get("ollama", {}) or {}
    host = o.get("host", "http://127.0.0.1:11434").rstrip("/")
    body = {"model": o.get("model", "qwen2.5:3b"), "messages": messages, "stream": False,
            "options": {"num_ctx": int(o.get("num_ctx", 6144))}}
    r = requests.post(host + "/api/chat", json=body, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Ollama HTTP {r.status_code}")
    text = ((r.json().get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty answer")
    return text


def _ask_model(system, task, cfg):
    """One call, no tools, on the owner's active brain. OmniRoute first when it is active, Ollama otherwise or on failure."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    errors = []
    if cfg.get("active_provider", "ollama") == "omniroute":
        try:
            return _ask_omniroute(messages, cfg)
        except Exception as e:  # noqa: BLE001 - fall back to Ollama
            errors.append(str(e))
    try:
        return _ask_ollama(messages, cfg)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    raise RuntimeError("; ".join(errors))


def run_specialist(name_or_query, task, cfg=None):
    cfg = cfg or cfg_mod.load_config()
    items = _index(cfg)
    if not items:
        return NO_PACKS
    task = (task or "").strip()
    if not task:
        return "Tell the specialist what to do: give a task."
    spec = _resolve(items, name_or_query)
    if not spec:
        return f"No specialist matches '{name_or_query}'. Search with find_specialist or list to see options."
    try:
        _, body = _read(spec["file"])
    except OSError as e:
        return f"Could not read the {spec['name']} profile: {e}"
    limit = int((cfg.get("specialists", {}) or {}).get("max_prompt_chars", DEFAULT_MAX_PROMPT))
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n...[truncated]"
    try:
        answer = _ask_model(body + SUFFIX, task, cfg)
    except Exception as e:  # noqa: BLE001 - report, never crash the tool layer
        return (f"[{spec['name']}]\nI could not get an answer ({e}). "
                "Check that OmniRoute or Ollama is running.")
    return f"[{spec['name']}]\n{answer}"


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "list":
        print(list_specialists(" ".join(argv[1:])))
    elif len(argv) >= 3 and argv[0] == "run":
        print(run_specialist(argv[1], " ".join(argv[2:])))
    else:
        print('usage: python va_specialists.py list [query] | run "<name or topic>" "<task>"')
        sys.exit(1)
