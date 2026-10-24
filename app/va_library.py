"""va_library.py - Valerie's offline knowledge library (Kiwix ZIM packs: offline Wikipedia, iFixit, medical...).

ensure_kiwix()          -> installs kiwix-serve + kiwix-search into DATA_DIR/library/bin (user-level, no sudo)
list_packs()            -> curated packs with approximate sizes, read from the official Kiwix listing
download_pack(name)     -> resumable download into DATA_DIR/library/zim/ (refuses if < 5 GB would stay free)
start_library()         -> kiwix-serve on 127.0.0.1:8095 in the background (pid file in library/)
stop_library()          -> stops it
library_status()        -> running or not, packs on disk, disk space
search_library(q, k)    -> titles + snippets + local URLs
read_article(x, n)      -> plain text of one article (URL or title)

CLI: python va_library.py packs | get <name> | start | stop | status | search "q" | read <url>
"""
import glob, html, os, re, shutil, signal, subprocess, sys, tarfile, tempfile, time
import xml.etree.ElementTree as ET

import requests

import config as cfg_mod

LIB_DIR = os.path.join(cfg_mod.DATA_DIR, "library")
ZIM_DIR = os.path.join(LIB_DIR, "zim")
BIN_DIR = os.path.join(LIB_DIR, "bin")
PID_PATH = os.path.join(LIB_DIR, "kiwix-serve.pid")
LOG_PATH = os.path.join(LIB_DIR, "kiwix-serve.log")

PORT = 8095
BASE = "http://127.0.0.1:%d" % PORT
KIWIX_TOOLS_URL = "https://download.kiwix.org/release/kiwix-tools/"
ZIM_ROOT_URL = "https://download.kiwix.org/zim/"
MIN_FREE_AFTER = 5 * 1024 ** 3          # never leave less than 5 GB free on the disk
ZIM_MAGIC = b"\x5a\x49\x4d\x04"          # every ZIM file starts with this

# name, Kiwix category folder, filename pattern (newest date wins), description
PACKS = [
    ("wikipedia_en_100_mini", "wikipedia", r"wikipedia_en_100_(\d{4}-\d{2})\.zim",
     "The 100 most important Wikipedia articles. Tiny, good for a first test."),
    ("wikipedia_en_chemistry_mini", "wikipedia", r"wikipedia_en_chemistry_mini_(\d{4}-\d{2})\.zim",
     "Chemistry articles from Wikipedia (short versions). Small test pack."),
    ("wikipedia_en_top_mini", "wikipedia", r"wikipedia_en_top_mini_(\d{4}-\d{2})\.zim",
     "Popular Wikipedia articles (short versions)."),
    ("wikipedia_en_medicine_nopic", "wikipedia", r"wikipedia_en_medicine_nopic_(\d{4}-\d{2})\.zim",
     "Medical Wikipedia articles without images (WikiMed-style medical reference)."),
    ("ifixit_en_all", "ifixit", r"ifixit_en_all_(\d{4}-\d{2})\.zim",
     "iFixit repair guides, step by step, all topics."),
]

_LISTING_CACHE = {}


# ---------- small helpers ----------

def _ensure_dirs():
    os.makedirs(ZIM_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)


def _fmt(n):
    if n >= 1024 ** 3:
        return "%.1f GB" % (n / 1024.0 ** 3)
    return "%d MB" % round(n / 1024.0 ** 2)


def _parse_size(text):
    m = re.match(r"([\d.]+)([KMGT]?)", text)
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _plain(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _free_bytes():
    _ensure_dirs()
    return shutil.disk_usage(LIB_DIR).free


def _listing(url):
    """Fetch a directory listing (cached for 10 minutes per call site)."""
    hit = _LISTING_CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    r = requests.get(url, timeout=(15, 60))
    r.raise_for_status()
    _LISTING_CACHE[url] = (time.time(), r.text)
    return r.text


def _resolve(name):
    """Find the newest file on the Kiwix site for a curated pack. Raises KeyError/ValueError."""
    spec = [p for p in PACKS if p[0] == name]
    if not spec:
        raise KeyError(name)
    _, cat, rx, _ = spec[0]
    folder = ZIM_ROOT_URL + cat + "/"
    best = None
    for href, when, size in re.findall(r'<a href="([^"?/]+)">[^<]*</a>\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+([\d.]+[KMGT]?)',
                                       _listing(folder)):
        m = re.fullmatch(rx, href)
        if m and (best is None or (m.group(1), when) > best[0]):
            best = ((m.group(1), when), href, _parse_size(size))
    if best is None:
        raise ValueError("no file matching the pattern on the Kiwix site")
    return {"file": best[1], "url": folder + best[1], "approx": best[2], "date": best[0][0]}


def _remote_size(url):
    r = requests.get(url, stream=True, timeout=(15, 60))
    with r:
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
    if not cl or not cl.isdigit():
        raise ValueError("the server didn't say how big the file is")
    return int(cl)


# ---------- kiwix-tools install ----------

def _bin(name):
    return os.path.join(BIN_DIR, name)


def _have_kiwix():
    return all(os.path.isfile(_bin(n)) and os.access(_bin(n), os.X_OK) for n in ("kiwix-serve", "kiwix-search"))


def _kiwix_version():
    try:
        out = subprocess.run([_bin("kiwix-serve"), "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout + out.stderr).strip().splitlines()[0]
    except Exception:
        return ""


def ensure_kiwix():
    """Install kiwix-serve and kiwix-search into library/bin if they are missing."""
    _ensure_dirs()
    if _have_kiwix():
        return "kiwix-tools is already installed (%s)." % (_kiwix_version() or "version unknown")
    try:
        listing = _listing(KIWIX_TOOLS_URL)
    except Exception as e:
        return "Couldn't reach the Kiwix download site to get kiwix-tools: %s" % e
    versions = re.findall(r"kiwix-tools_linux-x86_64-(\d+(?:\.\d+)+)\.tar\.gz", listing)
    if not versions:
        return "Couldn't find a kiwix-tools Linux x86_64 build on the Kiwix site."
    ver = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    archive_name = "kiwix-tools_linux-x86_64-%s.tar.gz" % ver
    tmp = tempfile.mkdtemp(prefix="kiwix-dl-", dir=LIB_DIR)
    try:
        archive = os.path.join(tmp, archive_name)
        r = requests.get(KIWIX_TOOLS_URL + archive_name, stream=True, timeout=(15, 120))
        with r:
            r.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        found = {}
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                base = os.path.basename(m.name)
                if (m.isfile() and base in ("kiwix-serve", "kiwix-search")
                        and not m.name.startswith("/") and ".." not in m.name.split("/")):
                    found[base] = tf.extractfile(m).read()
        if len(found) != 2:
            return "The kiwix-tools archive didn't contain kiwix-serve and kiwix-search."
        for base, data in found.items():
            path = _bin(base)
            with open(path + ".new", "wb") as f:
                f.write(data)
            os.chmod(path + ".new", 0o755)
            os.replace(path + ".new", path)
    except Exception as e:
        return "kiwix-tools install failed: %s" % e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not _have_kiwix():
        return "kiwix-tools install failed: binaries missing after extraction."
    return "Installed kiwix-tools %s into %s (%s)." % (ver, BIN_DIR, _kiwix_version() or "version unknown")


# ---------- catalog and downloads ----------

def list_packs():
    lines = ["Offline library packs (sizes are from the Kiwix site, approximate):"]
    for name, _, _, desc in PACKS:
        try:
            info = _resolve(name)
            lines.append("- %s: %s. %s" % (name, _fmt(info["approx"]), desc))
        except Exception as e:
            lines.append("- %s: couldn't check the size (%s). %s" % (name, e, desc))
    lines.append("- Not in this list: full English Wikipedia without images (wikipedia_en_all_nopic) is about 49 GB.")
    lines.append("Download one with: get <name>.")
    return "\n".join(lines)


def _download_resumable(url, part, have, total):
    """Stream url into the .part file, continuing from `have` bytes. Returns (ok, message)."""
    headers = {"Range": "bytes=%d-" % have} if have else {}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=(15, 60))
    except requests.RequestException as e:
        return False, "Download didn't start (%s). Run it again to resume." % e
    with r:
        if have and r.status_code == 416:
            os.remove(part)
            return False, "The server rejected the resume, so the partial file was removed. Run it again."
        if have and r.status_code == 206:
            mode, done = "ab", have
        elif r.status_code == 200:
            mode, done = "wb", 0
        else:
            return False, "The server answered HTTP %d." % r.status_code
        last = -1
        try:
            with open(part, mode) as f:
                for chunk in r.iter_content(1 << 20):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 100
                    if pct != last:
                        last = pct
                        sys.stderr.write("\r  %3d%%  %s of %s" % (pct, _fmt(done), _fmt(total)))
                        sys.stderr.flush()
        except (requests.RequestException, OSError) as e:
            return False, "\nDownload interrupted at %s of %s (%s). Run it again to resume." % (
                _fmt(done), _fmt(total), e)
    sys.stderr.write("\n")
    return True, ""


def download_pack(name, confirm=True):
    _ensure_dirs()
    try:
        info = _resolve(name)
    except KeyError:
        return "Unknown pack '%s'. Packs: %s" % (name, ", ".join(p[0] for p in PACKS))
    except Exception as e:
        return "Couldn't look up '%s' on the Kiwix site: %s" % (name, e)
    dest = os.path.join(ZIM_DIR, info["file"])
    part = dest + ".part"
    try:
        total = _remote_size(info["url"])
    except Exception as e:
        return "Couldn't get the size of %s: %s" % (info["file"], e)
    if os.path.isfile(dest) and os.path.getsize(dest) == total:
        return "%s is already downloaded (%s)." % (info["file"], _fmt(total))
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have > total:
        os.remove(part)
        have = 0
    need = total - have
    free = _free_bytes()
    if free - need < MIN_FREE_AFTER:
        return ("Not downloading %s: it needs %s more and only %s is free, so less than 5 GB would be left."
                % (info["file"], _fmt(need), _fmt(free)))
    if not confirm:
        return ("Ready to download %s (%s) into %s. Free space after: %s. Run again with confirm=True to start."
                % (info["file"], _fmt(total), ZIM_DIR, _fmt(free - need)))
    ok, msg = _download_resumable(info["url"], part, have, total)
    if not ok:
        return msg.strip()
    if os.path.getsize(part) != total:
        return "Size check failed for %s: got %d bytes, expected %d. Kept the partial file; run again." % (
            info["file"], os.path.getsize(part), total)
    with open(part, "rb") as f:
        if f.read(4) != ZIM_MAGIC:
            os.remove(part)
            return "%s isn't a valid ZIM file; the partial download was removed." % info["file"]
    os.replace(part, dest)
    result = "Downloaded %s (%s) to %s." % (info["file"], _fmt(total), ZIM_DIR)
    if _running_pid():
        stop_library()
        result += " " + start_library()
    else:
        result += " Start the library to use it."
    return result


# ---------- server ----------

def _running_pid():
    """pid of our kiwix-serve, or None (checks /proc so a reused pid is never mistaken for it)."""
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            if b"kiwix-serve" in f.read():
                return pid
    except (OSError, ValueError):
        pass
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    return None


def _port_up():
    try:
        requests.get(BASE + "/", timeout=3)
        return True
    except requests.RequestException:
        return False


def _zim_files():
    zims = set(glob.glob(os.path.join(ZIM_DIR, "*.zim")))
    extra_dir = cfg_mod.CONFIG.get("library", {}).get("extra_zim_dir", "/media/wwm/16D4253ED425218B/Offline-Library")
    if extra_dir and os.path.isdir(extra_dir):
        for z in glob.glob(os.path.join(extra_dir, "*.zim")):
            zims.add(z)
    return sorted(list(zims))


def start_library():
    _ensure_dirs()
    if _running_pid() and _port_up():
        return "The library is already running at %s." % BASE
    if not _have_kiwix():
        msg = ensure_kiwix()
        if not _have_kiwix():
            return "The library can't start because kiwix-tools isn't installed. " + msg
    zims = _zim_files()
    if not zims:
        return "No packs are downloaded yet. Use get <name> first (see packs)."
    with open(LOG_PATH, "ab") as log:
        proc = subprocess.Popen(
            [_bin("kiwix-serve"), "--address=127.0.0.1", "--port=%d" % PORT] + zims,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    with open(PID_PATH, "w") as f:
        f.write(str(proc.pid))
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            return "kiwix-serve exited right away (code %s). See %s." % (proc.returncode, LOG_PATH)
        if _port_up():
            return "The library is running at %s with %d pack(s)." % (BASE, len(zims))
        time.sleep(0.5)
    return "kiwix-serve started but didn't answer within 15 seconds. See %s." % LOG_PATH


def stop_library():
    pid = _running_pid()
    if not pid:
        return "The library isn't running."
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and _running_pid():
        time.sleep(0.2)
    if _running_pid():
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    return "The library is stopped."


def library_status():
    _ensure_dirs()
    pid = _running_pid()
    if pid and _port_up():
        state = "running at %s" % BASE
    else:
        state = "not running"
    zims = _zim_files()
    lines = ["Library: %s." % state,
             "kiwix-tools: %s." % ("installed" if _have_kiwix() else "not installed yet"),
             "Free disk space: %s." % _fmt(_free_bytes())]
    if zims:
        lines.append("Packs on disk:")
        for z in zims:
            lines.append("- %s (%s)" % (os.path.basename(z), _fmt(os.path.getsize(z))))
    else:
        lines.append("No packs on disk yet.")
    return "\n".join(lines)


def _ensure_running():
    if _running_pid() and _port_up():
        return True, ""
    msg = start_library()
    return (_running_pid() is not None and _port_up()), msg


# ---------- search and reading ----------

def _search_hits(query, k=5):
    r = requests.get(BASE + "/search", params={"pattern": query, "format": "xml", "pageLength": k}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)   # kiwix-serve answers with RSS 2.0: <item> with title, link, description
    hits = []
    for item in root.iter("item"):
        if len(hits) >= k:
            break
        title = (item.findtext("title") or "").strip()
        desc = item.find("description")
        snippet = _plain("".join(desc.itertext()) if desc is not None else "")   # snippet has <b> highlights inside
        link = (item.findtext("link") or "").strip()
        if link.startswith("/"):
            link = BASE + link
        hits.append({"title": title, "snippet": snippet, "url": link})
    return hits


def search_library(query, k=5):
    ok, msg = _ensure_running()
    if not ok:
        return "The library isn't running. " + msg
    try:
        hits = _search_hits(query, k)
    except Exception as e:
        return "Search failed: %s" % e
    if not hits:
        return "No results for '%s'." % query
    lines = []
    for i, h in enumerate(hits, 1):
        snip = h["snippet"][:240] + ("..." if len(h["snippet"]) > 240 else "")
        lines.append("%d. %s\n   %s\n   %s" % (i, h["title"], snip, h["url"]))
    return "\n".join(lines)


def read_article(url_or_title, max_chars=4000):
    ok, msg = _ensure_running()
    if not ok:
        return "The library isn't running. " + msg
    target = (url_or_title or "").strip()
    if re.match(r"https?://", target):
        url = target
    elif target.startswith("/"):
        url = BASE + target
    else:
        try:
            hits = _search_hits(target, 1)
        except Exception as e:
            return "Search failed: %s" % e
        if not hits or not hits[0]["url"]:
            return "No article found for '%s'." % target
        url = hits[0]["url"]
    try:
        r = requests.get(url, timeout=30)
    except requests.RequestException as e:
        return "Couldn't open %s: %s" % (url, e)
    if r.status_code == 404:
        return "No article at %s." % url
    if r.status_code != 200:
        return "The library answered HTTP %d for %s." % (r.status_code, url)
    m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    title = _plain(m.group(1)) if m else target
    body = _plain(re.sub(r"(?is)<head.*?</head>", " ", r.text))
    if len(body) > max_chars:
        body = body[:max_chars].rsplit(" ", 1)[0] + " ..."
    return "%s\n\n%s" % (title, body)


# ---------- CLI ----------

USAGE = "usage: va_library.py packs | get <name> | start | stop | status | search \"q\" | read <url or title>"


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "packs":
        print(list_packs())
    elif cmd == "get" and rest:
        print(download_pack(rest[0], confirm=True))
    elif cmd == "start":
        print(start_library())
    elif cmd == "stop":
        print(stop_library())
    elif cmd == "status":
        print(library_status())
    elif cmd == "search" and rest:
        print(search_library(" ".join(rest)))
    elif cmd == "read" and rest:
        print(read_article(" ".join(rest)))
    else:
        print(USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
