"""
knowledge.py - Valerie's local knowledge base (offline retrieval).

Indexes every .md / .txt / .docx / .json / .csv file in the folders
listed under cfg["knowledge"]["dirs"], splits them into passages, and
ranks passages against a question with BM25. The best passages are
handed to the model with each message, so even a small local model can
answer from the owner's own documents.

No extra packages: .docx text is read straight out of the zip's XML.
The index is cached in kb_index.json and only changed files are
re-read, so re-indexing after adding a document is fast.
"""

import html
import json
import math
import os
import re
import zipfile
from collections import Counter

import config as cfg_mod

EXTS = {".md", ".txt", ".docx", ".json", ".csv"}
CHUNK_CHARS = 900
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_STOP = set("""a an and are as at be but by for from has have i if in into is it its of on or our
that the their them then there these they this to was we were what when where which who will
with you your can do does how me my not so than about also just more any all""".split())

_cache = {"index": None}


def _tokens(text):
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def _read_docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def _read_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _read_docx(path)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _chunk(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = ""
        while len(p) > CHUNK_CHARS:
            chunks.append(p[:CHUNK_CHARS])
            p = p[CHUNK_CHARS:]
        cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _iter_files(cfg):
    kcfg = cfg.get("knowledge", {})
    max_bytes = kcfg.get("max_file_kb", 2048) * 1024
    for root_dir in kcfg.get("dirs", []):
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]  # skip .versions backups
            for name in files:
                if name.startswith(("~$", ".")):
                    continue
                if os.path.splitext(name)[1].lower() not in EXTS:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_size <= max_bytes:
                    yield path, st.st_mtime


def build_index(cfg=None, progress_cb=None):
    """(Re)build the index, re-reading only files whose mtime changed."""
    cfg = cfg or cfg_mod.load_config()
    old = {}
    if os.path.exists(cfg_mod.KB_INDEX_PATH):
        try:
            with open(cfg_mod.KB_INDEX_PATH) as f:
                old = {d["path"]: d for d in json.load(f).get("docs", [])}
        except (OSError, ValueError, KeyError):
            old = {}

    docs, errors = [], 0
    for path, mtime in _iter_files(cfg):
        prev = old.get(path)
        if prev and prev.get("mtime") == mtime:
            docs.append(prev)
            continue
        try:
            text = _read_file(path)
        except Exception:
            errors += 1
            continue
        docs.append({"path": path, "mtime": mtime, "chunks": _chunk(text)})
        if progress_cb:
            progress_cb(f"Indexed {os.path.basename(path)}")

    with open(cfg_mod.KB_INDEX_PATH, "w") as f:
        json.dump({"docs": docs}, f)
    _cache["index"] = None
    n_chunks = sum(len(d["chunks"]) for d in docs)
    return {"files": len(docs), "passages": n_chunks, "errors": errors}


def _load():
    if _cache["index"] is not None:
        return _cache["index"]
    if not os.path.exists(cfg_mod.KB_INDEX_PATH):
        build_index()
    try:
        with open(cfg_mod.KB_INDEX_PATH) as f:
            docs = json.load(f).get("docs", [])
    except (OSError, ValueError):
        docs = []

    passages = []
    for d in docs:
        title = os.path.splitext(os.path.basename(d["path"]))[0]
        for c in d["chunks"]:
            toks = _tokens(f"{title} {c}")
            passages.append({"source": d["path"], "title": title, "text": c,
                             "tf": Counter(toks), "len": len(toks)})
    df = Counter()
    for p in passages:
        df.update(p["tf"].keys())
    avg = (sum(p["len"] for p in passages) / len(passages)) if passages else 1
    _cache["index"] = {"passages": passages, "df": df, "avg": avg, "n": len(passages)}
    return _cache["index"]


def ecosystem_of(path, cfg=None):
    """Which workspace a document belongs to (or 'shared') - see workspaces.classify_path."""
    import workspaces
    return workspaces.classify_path(cfg or cfg_mod.load_config(), path)


def search(query, k=4, ecosystem=None):
    """Return up to k best passages: [{title, source, text, score}].
    ecosystem=<workspace id> leaves out the OTHER workspaces' documents."""
    idx = _load()
    q = set(_tokens(query))
    if not q or not idx["n"]:
        return []
    k1, b = 1.5, 0.75
    scored = []
    cfg = cfg_mod.load_config()
    tags = idx.setdefault("_tags", {})
    for p in idx["passages"]:
        if ecosystem and ecosystem != "both":
            tag = tags.get(p["source"])
            if tag is None:
                tag = tags[p["source"]] = ecosystem_of(p["source"], cfg)
            if tag not in ("shared", ecosystem):
                continue
        s = 0.0
        for t in q:
            f = p["tf"].get(t)
            if not f:
                continue
            idf = math.log(1 + (idx["n"] - idx["df"][t] + 0.5) / (idx["df"][t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * p["len"] / idx["avg"]))
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"title": p["title"], "source": p["source"], "text": p["text"], "score": round(s, 2)}
            for s, p in scored[:k]]


def context_for(query, cfg, ecosystem=None):
    """Formatted knowledge block for the system prompt ('' if nothing relevant)."""
    kcfg = cfg.get("knowledge", {})
    hits = search(query, kcfg.get("top_k", 4), ecosystem=ecosystem)
    budget = kcfg.get("max_context_chars", 3500)
    out = []
    for h in hits:
        block = f"[{h['title']}]\n{h['text'].strip()}"
        if len(block) > budget:
            break
        out.append(block)
        budget -= len(block)
    return "\n\n".join(out)


def stats():
    idx = _load()
    files = len({p["source"] for p in idx["passages"]})
    return {"files": files, "passages": idx["n"]}


def add_note(text):
    """Append a dated note to knowledge/notes.md (Valerie's long-term memory)."""
    import datetime
    os.makedirs(cfg_mod.KNOWLEDGE_DIR, exist_ok=True)
    new = not os.path.exists(cfg_mod.NOTES_PATH)
    with open(cfg_mod.NOTES_PATH, "a") as f:
        if new:
            f.write("# Valerie's notes\n\nThings I was asked to remember.\n\n")
        f.write(f"- {datetime.date.today().isoformat()}: {text.strip()}\n")
    build_index()
