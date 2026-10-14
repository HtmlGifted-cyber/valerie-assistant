"""va_docs.py - Valerie's offline "ask your documents" search.

index_docs(folders=None, progress=None) -> summary of what was (re)indexed
search_docs(query, k=5)                 -> numbered hits: file path, snippet, cosine score
add_docs_folder(path)                   -> adds a folder to the saved document folder list
remove_docs_folder(path)                -> removes it from the list and drops its index rows
docs_status()                           -> folders, file/chunk counts, last indexed time

Embeddings come from a local Ollama embed model (default nomic-embed-text), so document
text never leaves the machine. The index lives in DATA_DIR/docs_index.sqlite.

CLI (run with the Valerie venv):
  python va_docs.py index [folder...]    (folders given here are NOT saved to config)
  python va_docs.py search "question"
  python va_docs.py status
"""
import html, os, re, shutil, sqlite3, subprocess, sys, time

import numpy as np
import requests

import config as cfg_mod

DB_PATH = os.path.join(cfg_mod.DATA_DIR, "docs_index.sqlite")
CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
EMBED_BATCH = 16
SNIPPET_CHARS = 300
SUBPROCESS_TIMEOUT = 60
EMBED_TIMEOUT = 120
DEFAULT_EXTS = [".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".html", ".rtf", ".odt"]
SKIP_DIRS = {"node_modules", "venv", "__pycache__"}  # hidden dirs (.git, .venv, ...) are skipped by prefix
MARKITDOWN_PATH = os.path.expanduser("~/.local/bin/markitdown")


class DocsError(Exception):
    pass


def _settings():
    cfg = cfg_mod.load_config()
    d = cfg.get("docs", {}) or {}
    ollama_host = (cfg.get("ollama") or {}).get("host") or "http://127.0.0.1:11434"
    return {
        "folders": list(d.get("folders", []) or []),
        "embed_model": d.get("embed_model") or "nomic-embed-text",
        "ollama_host": (d.get("ollama_host") or ollama_host).rstrip("/"),
        "max_file_mb": d.get("max_file_mb", 25),
        "extensions": [e.lower() for e in (d.get("extensions") or DEFAULT_EXTS)],
    }


def _db():
    os.makedirs(cfg_mod.DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, chunks INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, "
                "pos INTEGER, text TEXT, vec BLOB)")
    con.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


# ---------- text extraction ----------

def _extract(path, ext):
    """Return plain text for a file, or None if it can't be read."""
    try:
        if ext in (".txt", ".md", ".csv"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        if ext == ".html":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
            raw = re.sub(r"<[^>]+>", " ", raw)
            return html.unescape(raw)
        if ext == ".pdf":
            exe = shutil.which("pdftotext")
            if not exe:
                return None
            r = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
            return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
        exe = shutil.which("markitdown") or MARKITDOWN_PATH
        if not os.path.exists(exe):
            return None
        r = subprocess.run([exe, path], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
        return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _chunk(text):
    """Split into ~800-char pieces with 150 chars of overlap. Returns [(char_offset, piece), ...]."""
    text = text.replace("\r\n", "\n")
    out = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    for start in range(0, max(len(text), 1), step):
        piece = text[start:start + CHUNK_CHARS]
        if piece.strip():
            out.append((start, piece))
        if start + CHUNK_CHARS >= len(text):
            break
    return out


# ---------- embeddings ----------

def _missing_model_msg(model):
    return f"The embed model '{model}' isn't pulled yet. Run: ollama pull {model}"


def _post(s, path, payload):
    try:
        return requests.post(s["ollama_host"] + path, json=payload, timeout=EMBED_TIMEOUT)
    except requests.RequestException:
        raise DocsError(f"Ollama isn't reachable at {s['ollama_host']}. Start Ollama and try again.")


def _check(s, r):
    if r.status_code == 404:
        raise DocsError(_missing_model_msg(s["embed_model"]))
    if r.status_code != 200:
        try:
            msg = r.json().get("error", r.text)
        except ValueError:
            msg = r.text
        raise DocsError(f"Ollama error: {msg}")


def _embed(s, texts):
    """Return a float32 matrix with one row per input text."""
    model = s["embed_model"]
    vecs = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        r = _post(s, "/api/embed", {"model": model, "input": batch})
        if r.status_code == 404:
            # Older Ollama builds only have /api/embeddings, one prompt at a time.
            for t in batch:
                r2 = _post(s, "/api/embeddings", {"model": model, "prompt": t})
                _check(s, r2)
                vecs.append(r2.json()["embedding"])
            continue
        _check(s, r)
        vecs.extend(r.json()["embeddings"])
    if len(vecs) != len(texts):
        raise DocsError("Ollama returned fewer embeddings than expected.")
    return np.asarray(vecs, dtype=np.float32)


# ---------- indexing ----------

def _walk(root, exts):
    """Yield (path, ext) for candidate files under root. Never follows symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in SKIP_DIRS
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            yield path, ext


def _forget(con, path):
    con.execute("DELETE FROM chunks WHERE path=?", (path,))
    con.execute("DELETE FROM files WHERE path=?", (path,))


def index_docs(folders=None, progress=None):
    """Index (or incrementally re-index) document folders. folders=None uses the saved list."""
    s = _settings()
    if folders is None:
        folders = s["folders"]
    roots, notes = [], []
    for f in folders:
        p = _real(f)
        if not os.path.isdir(p):
            notes.append(f"Folder not found: {f}")
        elif p not in roots:
            roots.append(p)
    if not roots:
        return "No document folders to index. Add one with add_docs_folder(path)." + \
            ("".join(" " + n + "." for n in notes) if notes else "")

    exts = set(s["extensions"])
    max_bytes = int(float(s["max_file_mb"]) * 1024 * 1024)
    candidates = [(root, path, ext) for root in roots for path, ext in _walk(root, exts)]
    total = len(candidates)

    indexed = chunks_total = skipped = removed = unchanged = 0
    seen = set()
    con = _db()
    try:
        stored = {row[0]: (row[1], row[2]) for row in con.execute("SELECT path, mtime, size FROM files")}
        for n, (root, path, ext) in enumerate(candidates, 1):
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                skipped += 1
                continue
            if progress:
                progress(f"[{n}/{total}] {os.path.basename(path)}")
            old = stored.get(path)
            if old and old[0] == st.st_mtime and old[1] == st.st_size:
                unchanged += 1
                continue
            if st.st_size > max_bytes:
                _forget(con, path)
                con.commit()
                skipped += 1
                continue
            text = _extract(path, ext)
            pieces = _chunk(text) if text else []
            if not pieces:
                _forget(con, path)
                con.commit()
                skipped += 1
                continue
            try:
                vecs = _embed(s, [body for _, body in pieces])
            except DocsError as e:
                con.rollback()
                notes.insert(0, f"Stopped at {path}: {e}")
                break
            con.execute("DELETE FROM chunks WHERE path=?", (path,))
            con.executemany("INSERT INTO chunks (path, pos, text, vec) VALUES (?,?,?,?)",
                            [(path, pos, body, vec.tobytes()) for (pos, body), vec in zip(pieces, vecs)])
            con.execute("INSERT OR REPLACE INTO files (path, mtime, size, chunks) VALUES (?,?,?,?)",
                        (path, st.st_mtime, st.st_size, len(pieces)))
            con.commit()
            indexed += 1
            chunks_total += len(pieces)

        # drop rows for files that were deleted (or are no longer indexable) under the folders we just walked
        for path in list(stored):
            if path not in seen and any(path.startswith(r + os.sep) for r in roots):
                _forget(con, path)
                removed += 1
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)", (str(time.time()),))
        con.commit()
    finally:
        con.close()

    msg = f"Indexed {indexed} files ({chunks_total} chunks), {skipped} skipped, {removed} removed."
    if unchanged:
        msg += f" {unchanged} unchanged."
    if notes:
        msg += " " + " ".join(notes)
    return msg


# ---------- search ----------

def search_docs(query, k=5):
    query = (query or "").strip()
    if not query:
        return "Tell me what to look for."
    s = _settings()
    con = _db()
    try:
        rows = con.execute("SELECT path, pos, text, vec FROM chunks").fetchall()
    finally:
        con.close()
    if not rows:
        return "No documents are indexed yet. Add a folder with add_docs_folder(path) and run index."
    try:
        q = _embed(s, [query])[0]
    except DocsError as e:
        return str(e)
    dim = q.shape[0]
    rows = [r for r in rows if len(r[3]) == dim * 4]
    if not rows:
        return "The index was built with a different embed model. Re-index the document folders."
    M = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
    denom = np.linalg.norm(M, axis=1) * np.linalg.norm(q)
    denom[denom == 0] = 1e-12
    scores = (M @ q) / denom
    top = np.argsort(-scores)[:max(1, int(k))]
    lines = [f"Top {len(top)} matches for '{query}':"]
    for i, idx in enumerate(top, 1):
        path, _pos, text, _vec = rows[idx]
        snip = " ".join(text.split())[:SNIPPET_CHARS]
        if len(text) > SNIPPET_CHARS:
            snip += "..."
        lines.append(f"{i}. {path} (score {scores[idx]:.3f})\n   {snip}")
    return "\n".join(lines)


# ---------- folder list and status ----------

def add_docs_folder(path):
    p = _real(path)
    if not os.path.isdir(p):
        return f"Not a folder: {path}"
    cfg = cfg_mod.load_config()
    docs = cfg.get("docs") or {}
    folders = list(docs.get("folders") or [])
    if p in [_real(f) for f in folders]:
        return f"{p} is already in the document folders."
    folders.append(p)
    docs["folders"] = folders
    cfg["docs"] = docs
    cfg_mod.save_config(cfg)
    return f"Added {p} to the document folders. Run index to index it."


def remove_docs_folder(path):
    p = _real(path)
    cfg = cfg_mod.load_config()
    docs = cfg.get("docs") or {}
    folders = list(docs.get("folders") or [])
    keep = [f for f in folders if _real(f) != p]
    if len(keep) != len(folders):
        docs["folders"] = keep
        cfg["docs"] = docs
        cfg_mod.save_config(cfg)
    con = _db()
    try:
        gone = [row[0] for row in con.execute("SELECT path FROM files") if row[0].startswith(p + os.sep)]
        for path_ in gone:
            _forget(con, path_)
        con.commit()
    finally:
        con.close()
    if len(keep) == len(folders) and not gone:
        return f"{p} wasn't in the document folders or the index."
    return f"Removed {p} from the document folders and dropped {len(gone)} files from the index."


def docs_status():
    s = _settings()
    con = _db()
    try:
        files = con.execute("SELECT path, chunks FROM files").fetchall()
        last = con.execute("SELECT value FROM meta WHERE key='last_indexed'").fetchone()
    finally:
        con.close()
    lines = []
    if s["folders"]:
        lines.append("Document folders:")
        for f in s["folders"]:
            p = _real(f)
            if not os.path.isdir(p):
                lines.append(f"  {f} (not found)")
                continue
            mine = [c for (path, c) in files if path.startswith(p + os.sep)]
            lines.append(f"  {p} - {len(mine)} files, {sum(mine)} chunks")
    else:
        lines.append("No document folders set.")
    lines.append(f"Index total: {len(files)} files, {sum(c for _, c in files)} chunks.")
    if last:
        lines.append("Last indexed: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last[0]))) + ".")
    else:
        lines.append("Last indexed: never.")
    lines.append(f"Embed model: {s['embed_model']} at {s['ollama_host']}.")
    return "\n".join(lines)


# ---------- CLI ----------

def _main(argv):
    if not argv or argv[0] not in ("index", "search", "status"):
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "index":
        folders = [os.path.abspath(a) for a in argv[1:]] or None
        print(index_docs(folders, progress=lambda m: print(m, file=sys.stderr)))
    elif cmd == "search":
        print(search_docs(" ".join(argv[1:])))
    else:
        print(docs_status())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
