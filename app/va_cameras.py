"""va_cameras.py - Valerie's local cameras (no cloud). Motion checks run on this laptop only.

find_cameras()                          -> USB webcams on this laptop + camera ports on the home network (no passwords tried)
add_camera(name, url, motion=True)      -> save a camera in config.json
remove_camera(name)                     -> remove a camera from config.json (pictures are kept)
snapshot(name)                          -> save one picture; returns the file path, or an error message
check_motion(name)                      -> take a picture and compare it with the last one; returns a plain-English result
watch_cameras(interval, stop_event, notify) -> check every camera with motion on, alert on motion
camera_status()                         -> cameras set up, last picture, motion events today

CLI: python va_cameras.py find | add <name> <url> [--no-motion] | remove <name> | snap <name> | watch | status
Pictures: DATA_DIR/cameras/<name>/ (latest.jpg plus motion pictures). Motion log: DATA_DIR/cameras/events.log.
"""
import base64
import concurrent.futures
import datetime
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

import config as cfg_mod

CAM_DIR = os.path.join(cfg_mod.DATA_DIR, "cameras")
EVENTS_LOG = os.path.join(CAM_DIR, "events.log")
KEEP_EVENTS = 500              # newest motion pictures kept per camera
PIXEL_CHANGE = 0.1             # a pixel counts as changed if its brightness moved by more than 10%
SMALL = (160, 90)              # pictures are compared at this size
FFMPEG = "/usr/bin/ffmpeg"
SCAN_PORTS = {554: "RTSP", 8554: "RTSP", 80: "HTTP", 8080: "HTTP", 8899: "HTTP/ONVIF"}
ROUTER_WORDS = ("router", "gateway", "tp-link", "netgear", "linksys", "asus", "mikrotik",
                "openwrt", "ubiquiti", "d-link")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
URL_OK = re.compile(r"^(rtsp|http|https)://\S+$|^/dev/video\d+$")
EVENT_IMG = re.compile(r"^\d{8}-\d{6}(-\d+)?\.jpg$")


def _sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def mask_url(text):
    """Hide user:pass@ in a URL (or in any text that contains one)."""
    return re.sub(r"(://)[^/\s]*@", r"\1***@", text or "")


def _auth_split(url):
    """Return (url without user:pass, Basic auth header or None)."""
    p = urllib.parse.urlsplit(url)
    if not p.username:
        return url, None
    netloc = p.netloc.rsplit("@", 1)[1]
    clean = urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    raw = "%s:%s" % (urllib.parse.unquote(p.username), urllib.parse.unquote(p.password or ""))
    return clean, "Basic " + base64.b64encode(raw.encode()).decode()


def _cams(cfg=None):
    cfg = cfg or cfg_mod.load_config()
    return list(cfg.get("cameras", []) or [])


def _find_cam(name, cfg=None):
    for c in _cams(cfg):
        if c.get("name") == name:
            return c
    return None


def _cam_path(name):
    return os.path.join(CAM_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "_", name))


def _cam_dir(name):
    d = _cam_path(name)
    os.makedirs(d, exist_ok=True)
    return d


def _names_text():
    names = [c.get("name", "?") for c in _cams()]
    return ", ".join(names) if names else "none yet"


# ---------- finding cameras ----------

def _sysfs_name(dev):
    try:
        with open("/sys/class/video4linux/%s/name" % os.path.basename(dev)) as f:
            return f.read().strip()
    except OSError:
        return ""


def _usb_cameras():
    """[(device path, name)] for /dev/video* devices."""
    names = {}
    if shutil.which("v4l2-ctl"):
        head = ""
        for line in _sh(["v4l2-ctl", "--list-devices"]).splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                head = line.rstrip()[:-1]
            elif line.strip().startswith("/dev/"):
                names[line.strip()] = head
    devs = sorted(glob.glob("/dev/video[0-9]*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    return [(d, names.get(d) or _sysfs_name(d) or "camera") for d in devs]


def _lan():
    """(this laptop's IP, default gateway IP) on the default network interface."""
    route = _sh(["ip", "-4", "route", "show", "default"])
    dev = re.search(r"\bdev (\S+)", route)
    gw = re.search(r"\bvia (\S+)", route)
    if not dev:
        return None, None
    addr = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", _sh(["ip", "-4", "-o", "addr", "show", "dev", dev.group(1)]))
    return (addr.group(1) if addr else None), (gw.group(1) if gw else None)


def _port_open(ip, port, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fingerprint(ip, port):
    """Plain GET / with no login: returns (Server header, page title, HTTP status code)."""
    try:
        req = urllib.request.Request("http://%s:%d/" % (ip, port), headers={"User-Agent": "Valerie-camera-scan"})
        try:
            resp = urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as e:
            resp = e
        code = getattr(resp, "status", None) or getattr(resp, "code", 0)
        server = resp.headers.get("Server", "") or ""
        body = resp.read(4096).decode("utf-8", "replace")
        resp.close()
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = " ".join(m.group(1).split())[:60] if m else ""
        return server, title, code
    except Exception:
        return "", "", 0


def find_cameras():
    lines = ["USB cameras on this laptop:"]
    usb = _usb_cameras()
    if not usb:
        lines.append("  none found")
    for dev, name in usb:
        ok = os.access(dev, os.R_OK | os.W_OK)
        lines.append("  %s - %s%s" % (dev, name, "" if ok else " (no permission for this user)"))
    ip, gw = _lan()
    if not ip:
        lines.append("Network: this laptop isn't on a network, so the network scan was skipped.")
        return "\n".join(lines)
    base = ip.rsplit(".", 1)[0]
    hosts = ["%s.%d" % (base, i) for i in range(1, 255) if "%s.%d" % (base, i) != ip]
    pairs = [(h, p) for h in hosts for p in SCAN_PORTS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        results = list(ex.map(lambda hp: _port_open(*hp), pairs))
    by_host = {}
    for (h, p), ok in zip(pairs, results):
        if ok:
            by_host.setdefault(h, []).append(p)
    hosts_open = sorted(by_host, key=lambda h: int(h.rsplit(".", 1)[1]))
    http_pairs = [(h, p) for h in hosts_open for p in by_host[h] if SCAN_PORTS[p].startswith("HTTP")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        fps = dict(zip(http_pairs, ex.map(lambda hp: _fingerprint(*hp), http_pairs)))
    # the router is only skipped for RTSP when its web page says it's a router
    gw_text = " ".join(" ".join(v[:2]) for k, v in fps.items() if k[0] == gw).lower()
    gw_is_router = bool(gw) and any(w in gw_text for w in ROUTER_WORDS)
    lines.append("")
    lines.append("Home network %s.0/24 (this laptop is %s):" % (base, ip))
    any_found = False
    for h in hosts_open:
        for p in sorted(by_host[h]):
            label = SCAN_PORTS[p]
            if h == gw and label == "RTSP" and gw_is_router:
                lines.append("  (%s: RTSP port skipped, it's the router)" % h)
                continue
            desc = "port %d open (%s)" % (p, label)
            info = fps.get((h, p))
            if info:
                server, title, code = info
                extras = []
                if server:
                    extras.append("Server: " + server)
                if title:
                    extras.append("title: " + title)
                if code == 401:
                    extras.append("needs a login")
                if extras:
                    desc += " - " + ", ".join(extras)
            if h == gw:
                desc += " [router]"
            lines.append("  %s: %s" % (h, desc))
            any_found = True
    if not any_found:
        lines.append("  no camera ports open on this network")
    lines.append("Only the usual camera ports were checked, and no passwords were tried.")
    return "\n".join(lines)


# ---------- configured cameras ----------

def add_camera(name, url, motion=True):
    name = (name or "").strip()
    url = (url or "").strip()
    if not NAME_OK.match(name):
        return "Camera names can use letters, numbers, spaces, dots, dashes and underscores (up to 40 characters)."
    if not URL_OK.match(url):
        return "The address must start with rtsp://, http:// or https://, or be a USB camera such as /dev/video0."
    cfg = cfg_mod.load_config()
    cams = _cams(cfg)
    if any(c.get("name") == name for c in cams):
        return "There is already a camera called '%s'. Remove it first if you want to change it." % name
    cams.append({"name": name, "url": url, "motion": bool(motion)})
    cfg["cameras"] = cams
    cfg_mod.save_config(cfg)
    return "Added camera '%s' (%s), motion alerts %s." % (name, mask_url(url), "on" if motion else "off")


def remove_camera(name):
    cfg = cfg_mod.load_config()
    cams = _cams(cfg)
    keep = [c for c in cams if c.get("name") != name]
    if len(keep) == len(cams):
        return "No camera called '%s'. Cameras set up: %s" % (name, _names_text())
    cfg["cameras"] = keep
    cfg_mod.save_config(cfg)
    return "Removed camera '%s'. Its pictures are still in %s." % (name, _cam_path(name))


# ---------- pictures ----------

def _grab(url, path):
    """Save one picture to path. Returns None on success, or a short error message."""
    if url.startswith("/dev/"):
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "v4l2", "-i", url, "-frames:v", "1", path]
    elif url.startswith("rtsp://"):
        cmd = [FFMPEG, "-y", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url, "-frames:v", "1", path]
    else:
        return _http_grab(url, path)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "no picture within 20 seconds (camera offline or busy?)"
    if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        lines = (r.stderr or "").strip().splitlines()
        return lines[-1] if lines else "ffmpeg could not get a picture"
    return None


def _http_grab(url, path):
    clean, auth = _auth_split(url)
    headers = {"User-Agent": "Valerie-camera"}
    if auth:
        headers["Authorization"] = auth
    try:
        req = urllib.request.Request(clean, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(20 * 1024 * 1024)
            ctype = resp.headers.get("Content-Type", "") or ""
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "camera asks for a login (put user:pass@ in the address)"
        return "camera answered HTTP %d" % e.code
    except Exception as e:
        return "could not reach the camera (%s)" % type(e).__name__
    if not data.startswith(b"\xff\xd8") and not ctype.startswith("image/"):
        return "camera sent %s, not a picture" % (ctype or "unknown data")
    with open(path, "wb") as f:
        f.write(data)
    return None


def snapshot(name):
    cam = _find_cam(name)
    if not cam:
        return "No camera called '%s'. Cameras set up: %s" % (name, _names_text())
    folder = _cam_dir(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, stamp + ".jpg")
    n = 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s-%d.jpg" % (stamp, n))
        n += 1
    err = _grab(cam.get("url", ""), path)
    if err:
        if os.path.exists(path):
            os.remove(path)
        return "Snapshot failed for '%s': %s" % (name, mask_url(err))
    return path


def _small(path):
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize(SMALL, Image.BILINEAR), dtype=np.float32) / 255.0


def _log_event(name, path, mean, frac):
    os.makedirs(CAM_DIR, exist_ok=True)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with open(EVENTS_LOG, "a") as f:
        f.write("%s\t%s\tmotion\t%s\tmean=%.3f\tfrac=%.3f\n" % (stamp, name, path, mean, frac))


def _trim_events(folder):
    imgs = sorted(f for f in os.listdir(folder) if EVENT_IMG.match(f))
    for f in imgs[:-KEEP_EVENTS]:
        os.remove(os.path.join(folder, f))


def _motion_check(name, cfg=None):
    """Returns (motion_found, kept_picture_path_or_None, message)."""
    cfg = cfg or cfg_mod.load_config()
    if not _find_cam(name, cfg):
        return False, None, "No camera called '%s'. Cameras set up: %s" % (name, _names_text())
    snap = snapshot(name)
    if not os.path.isfile(snap):
        return False, None, snap
    folder = _cam_dir(name)
    latest = os.path.join(folder, "latest.jpg")
    thr = float(cfg.get("motion_threshold", 0.06))
    new = _small(snap)
    if not os.path.exists(latest):
        shutil.copyfile(snap, latest)
        os.remove(snap)
        return False, None, ("First picture from '%s' saved as the starting point; "
                             "motion checks begin next time." % name)
    old = _small(latest)
    diff = np.abs(new - old)
    mean = float(diff.mean())
    frac = float((diff > PIXEL_CHANGE).mean())
    shutil.copyfile(snap, latest)
    detail = "%.1f%% of the picture changed, average change %.1f%%" % (frac * 100, mean * 100)
    if not (mean > thr or frac > thr):
        os.remove(snap)
        return False, None, "No motion on '%s' (%s)." % (name, detail)
    _log_event(name, snap, mean, frac)
    _trim_events(folder)
    return True, snap, "MOTION on '%s' (%s). Saved %s" % (name, detail, snap)


def check_motion(name):
    return _motion_check(name)[2]


def _desktop_alert(name, path):
    subprocess.run(["notify-send", "Motion: %s" % name], timeout=10)


def watch_cameras(interval=20, stop_event=None, notify=None):
    """Check every camera with motion on every `interval` seconds until stop_event is set."""
    notify = notify or _desktop_alert
    while not (stop_event and stop_event.is_set()):
        for cam in cfg_mod.load_config().get("cameras", []) or []:
            if stop_event and stop_event.is_set():
                break
            if not cam.get("motion"):
                continue
            name = cam.get("name", "")
            try:
                moved, path, msg = _motion_check(name)
            except Exception as e:
                print("Camera '%s' check failed: %s" % (name, type(e).__name__), file=sys.stderr, flush=True)
                continue
            if moved:
                print(msg, flush=True)
                try:
                    notify(name, path)
                except Exception:
                    pass
        if stop_event:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
    return "Camera watch stopped."


def camera_status():
    cams = _cams()
    if not cams:
        return "No cameras are set up yet. Run find to look for them, then add one."
    today = datetime.date.today().isoformat()
    events = {}
    if os.path.exists(EVENTS_LOG):
        with open(EVENTS_LOG) as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3 and p[0].startswith(today) and p[2] == "motion":
                    events[p[1]] = events.get(p[1], 0) + 1
    lines = ["%d camera(s):" % len(cams)]
    for c in cams:
        name = c.get("name", "?")
        latest = os.path.join(_cam_path(name), "latest.jpg")
        if os.path.exists(latest):
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(latest)))
        else:
            last = "no picture yet"
        lines.append("  %s - %s - motion %s - last picture %s - motion events today: %d" % (
            name, mask_url(c.get("url", "")), "on" if c.get("motion") else "off", last, events.get(name, 0)))
    return "\n".join(lines)


# ---------- command line ----------

USAGE = ("Usage: va_cameras.py find | add <name> <url> [--no-motion] | remove <name> | "
         "snap <name> | watch | status")


def main(argv):
    if not argv:
        print(USAGE)
        return 1
    cmd, args = argv[0], argv[1:]
    need = {"add": 2, "remove": 1, "snap": 1}
    if cmd not in ("find", "add", "remove", "snap", "watch", "status") or len(args) < need.get(cmd, 0):
        print(USAGE)
        return 1
    if cmd == "find":
        print(find_cameras())
    elif cmd == "add":
        print(add_camera(args[0], args[1], "--no-motion" not in args[2:]))
    elif cmd == "remove":
        print(remove_camera(args[0]))
    elif cmd == "snap":
        print(snapshot(args[0]))
    elif cmd == "watch":
        print("Watching cameras with motion on, every 20 seconds. Ctrl+C to stop.", flush=True)
        try:
            watch_cameras(20)
        except KeyboardInterrupt:
            print("Stopped.")
    elif cmd == "status":
        print(camera_status())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
