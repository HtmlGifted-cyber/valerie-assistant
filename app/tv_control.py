"""
tv_control.py - control a Roku TV on the home network (Roku External Control
Protocol, port 8060). Needs "Control by mobile apps" set to Enabled on the TV.

Config (config.json):  "tv": {"roku_ip": "<your TV's IP>"}
If roku_ip is missing, the TV is found with an SSDP search.
"""

import re
import socket
import time
from urllib.parse import quote

import requests

import config as cfg_mod

KEYS = {
    "power_on": "PowerOn", "power_off": "PowerOff", "power": "Power",
    "mute": "VolumeMute", "volume_up": "VolumeUp", "volume_down": "VolumeDown",
    "play_pause": "Play", "pause": "Play", "play": "Play",
    "rewind": "Rev", "fast_forward": "Fwd", "replay": "InstantReplay",
    "home": "Home", "back": "Back", "ok": "Select", "select": "Select",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
}
INPUTS = {"hdmi1": "InputHDMI1", "hdmi 1": "InputHDMI1", "hdmi2": "InputHDMI2", "hdmi 2": "InputHDMI2",
          "hdmi3": "InputHDMI3", "hdmi 3": "InputHDMI3", "live tv": "InputTuner", "antenna": "InputTuner",
          "av": "InputAV1"}


class TVError(Exception):
    pass


def _discover():
    msg = ("M-SEARCH * HTTP/1.1\r\nHost: 239.255.255.250:1900\r\nMan: \"ssdp:discover\"\r\n"
           "ST: roku:ecp\r\nMX: 2\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        data = s.recv(1024).decode(errors="ignore")
    except OSError:
        return None
    finally:
        s.close()
    m = re.search(r"LOCATION:\s*http://([\d.]+):8060", data, re.I)
    return m.group(1) if m else None


def _base():
    ip = cfg_mod.load_config().get("tv", {}).get("roku_ip") or _discover()
    if not ip:
        raise TVError("I can't find a Roku TV on the network.")
    return f"http://{ip}:8060"


def _post(path):
    try:
        r = requests.post(_base() + path, timeout=5)
    except requests.RequestException as e:
        raise TVError(f"the TV didn't answer ({e.__class__.__name__}). Is it on the same Wi-Fi?")
    if r.status_code == 403 or r.status_code == 401:
        raise TVError("the TV refused. On the TV: Settings > System > Advanced system settings > "
                      "Control by mobile apps > Enabled.")
    if r.status_code >= 400:
        raise TVError(f"the TV said error {r.status_code}.")


def _get(path):
    try:
        return requests.get(_base() + path, timeout=5).text
    except requests.RequestException as e:
        raise TVError(f"the TV didn't answer ({e.__class__.__name__}).")


def apps():
    xml = _get("/query/apps")
    return {name.replace("&amp;", "&"): aid for aid, name in re.findall(r'<app id="([^"]+)"[^>]*>([^<]+)</app>', xml)}


def _find_app(name):
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    found = apps()
    for title, aid in found.items():          # exact name first
        if re.sub(r"[^a-z0-9]", "", title.lower()) == want:
            return title, aid
    for title, aid in found.items():
        if re.sub(r"[^a-z0-9]", "", title.lower()).startswith(want):
            return title, aid
    for title, aid in found.items():
        if want in re.sub(r"[^a-z0-9]", "", title.lower()):
            return title, aid
    raise TVError(f"there's no '{name}' app on the TV. Installed: {', '.join(found)}.")


def status():
    info = _get("/query/device-info")
    power = (re.search(r"<power-mode>([^<]+)", info) or [None, "unknown"])[1]
    app = re.search(r"<app[^>]*>([^<]+)</app>", _get("/query/active-app"))
    on = power == "PowerOn"
    return f"The TV is {'on' if on else 'off/standby'}" + (f", showing {app.group(1).replace('&amp;', '&')}." if on and app else ".")


def control(action, app=None, times=1, text=None):
    """Run one TV action and return a short sentence for the chat."""
    action = (action or "").lower().strip().replace(" ", "_").replace("-", "_")
    times = max(1, min(int(times or 1), 20))
    if action == "status":
        return status()
    if action in ("open_app", "launch", "open"):
        if not app:
            raise TVError("which app? e.g. YouTube, Tubi, Plex.")
        if app.lower() in INPUTS:
            _post("/keypress/" + INPUTS[app.lower()])
            return f"Switched the TV to {app}."
        title, aid = _find_app(app)
        _post("/launch/" + aid)
        return f"Opening {title} on the TV."
    if action == "input":
        key = INPUTS.get((app or "").lower())
        if not key:
            raise TVError(f"unknown input '{app}'. Try HDMI 1, HDMI 2, HDMI 3, Live TV or AV.")
        _post("/keypress/" + key)
        return f"Switched the TV to {app}."
    if action == "type":
        for ch in (text or ""):
            _post("/keypress/Lit_" + quote(ch, safe=""))
        return f"Typed '{text}' on the TV."
    key = KEYS.get(action)
    if not key:
        raise TVError(f"I don't know the TV action '{action}'.")
    for i in range(times):
        _post("/keypress/" + key)
        if times > 1:
            time.sleep(0.15)
    words = {"power_on": "Turned the TV on.", "power_off": "Turned the TV off.", "mute": "Pressed mute on the TV.",
             "volume_up": f"TV volume up {times}.", "volume_down": f"TV volume down {times}.",
             "play_pause": "Pressed play/pause.", "pause": "Pressed pause.", "play": "Pressed play.",
             "home": "TV is on the home screen."}
    return words.get(action, f"Pressed {key}" + (f" {times} times." if times > 1 else "."))


# ---- plain-English shortcut (used by the chat window, no AI needed) --------

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def parse(lower):
    """Turn 'turn the tv volume up 3' into (action, app, times) or None.
    Only fires when the sentence mentions the TV (or Roku)."""
    if not re.search(r"\b(tv|t\.v\.|television|roku)\b", lower):
        return None
    n = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b(?! ?(am|pm))", lower)
    times = int(n.group(1)) if n and n.group(1).isdigit() else _NUM.get(n.group(1), 1) if n else 1
    m = re.search(r"\b(?:put on|open|launch|start|switch to|go to|play)\s+(.+?)\s+(?:on|in)\s+(?:the )?(?:tv|television|roku)\b", lower) \
        or re.search(r"\b(?:tv|television|roku)\s*,?\s*(?:put on|open|launch|start|switch to|go to)\s+(.+)$", lower)
    if m:
        return "open_app", m.group(1).strip(" ."), 1
    if re.search(r"\b(status|is the tv on|what'?s on)\b", lower):
        return "status", None, 1
    if re.search(r"\bun-?mute\b|\bmute\b", lower):
        return "mute", None, 1
    if re.search(r"\b(volume|sound) up\b|\bturn (it |the tv )?up\b|\blouder\b", lower):
        return "volume_up", None, times
    if re.search(r"\b(volume|sound) down\b|\bturn (it |the tv )?down\b|\bquieter\b|\bsofter\b", lower):
        return "volume_down", None, times
    if re.search(r"\b(turn|switch|power) (the )?(tv|television|roku) off\b|\b(tv|television) off\b|\bturn off (the )?(tv|television)\b", lower):
        return "power_off", None, 1
    if re.search(r"\b(turn|switch|power) (the )?(tv|television|roku) on\b|\b(tv|television) on\b|\bturn on (the )?(tv|television)\b", lower):
        return "power_on", None, 1
    if re.search(r"\b(pause|resume|play)\b(?! (on|up))", lower) and not re.search(r"\b(put on|open|launch|start)\b", lower):
        return "play_pause", None, 1
    if re.search(r"\bhome( screen)?\b", lower):
        return "home", None, 1
    return None
