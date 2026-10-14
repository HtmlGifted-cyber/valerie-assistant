"""va_network.py - Valerie's home-network guardian tools (generic: no personal data baked in).

network_check()  -> "why is my internet slow?": speed test, live laptop traffic, busiest programs,
                    Wi-Fi signal, devices on the network. Returns plain facts for Valerie to explain.
security_check() -> intrusion watch: listening ports vs the saved baseline, remote logins,
                    unknown devices, new startup programs.
Baselines live in ~/.local/share/valerie-assistant/guardian/ and are created on first run.
"""
import json, os, re, socket, subprocess, time

GUARD = os.path.expanduser("~/.local/share/ecohub/guardian")  # shared with Ecosystem Hub guardian
DASH_STATUS = "/var/www/html/eco/status.json"   # optional: Ecosystem Hub status file if present


def home_summary(max_age=1800):
    """One-line home-system summary from the status collector (works offline). None if missing or stale."""
    try:
        with open(DASH_STATUS) as f:
            d = json.load(f)
        if time.time() - d.get("generated", 0) > max_age or not d.get("summary"):
            return None
        return d["summary"].strip()
    except Exception:
        return None


def _sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _iface():
    route = _sh("ip route show default")
    m = re.search(r"dev (\S+)", route)
    return m.group(1) if m else None


def _bytes(iface):
    for line in open("/proc/net/dev"):
        if line.strip().startswith(iface + ":"):
            p = line.split(":")[1].split()
            return int(p[0]), int(p[8])
    return 0, 0


def _speed_test():
    """Small real test against Cloudflare (about 10 MB down, 2 MB up)."""
    out = {}
    d = _sh("curl -s -o /dev/null -w '%{speed_download} %{time_total}' "
            "'https://speed.cloudflare.com/__down?bytes=10000000' --max-time 40", timeout=45).split()
    if len(d) == 2 and float(d[0]) > 0:
        out["download_mbps"] = round(float(d[0]) * 8 / 1e6, 1)
    u = _sh("head -c 2000000 /dev/zero | curl -s -o /dev/null -w '%{speed_upload}' --data-binary @- "
            "https://speed.cloudflare.com/__up --max-time 40", timeout=45)
    try:
        if float(u) > 0:
            out["upload_mbps"] = round(float(u) * 8 / 1e6, 1)
    except ValueError:
        pass
    p = re.search(r"= [\d.]+/([\d.]+)/", _sh("ping -c 4 -q 1.1.1.1", timeout=15))
    if p:
        out["ping_ms"] = round(float(p.group(1)))
    return out


def network_check(run_speed_test=True):
    facts = []
    iface = _iface()
    if not iface:
        return "The laptop isn't connected to any network right now."
    wifi = _sh("nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi | grep '^yes'").split(":")
    if len(wifi) == 3:
        facts.append(f"Connected to Wi-Fi '{wifi[1]}' at {wifi[2]}% signal" +
                     (" (weak signal can cause slowness)" if wifi[2].isdigit() and int(wifi[2]) < 50 else ""))
    # live traffic on this laptop over 3 seconds
    r1, t1 = _bytes(iface)
    time.sleep(3)
    r2, t2 = _bytes(iface)
    down = (r2 - r1) * 8 / 3 / 1e6
    up = (t2 - t1) * 8 / 3 / 1e6
    facts.append(f"This laptop is using {down:.1f} Mbps down and {up:.1f} Mbps up right now")
    # programs with the most open internet connections
    conns = {}
    for m in re.finditer(r'users:\(\("([^"]+)"', _sh("ss -tunp state established")):
        conns[m.group(1)] = conns.get(m.group(1), 0) + 1
    if conns:
        top = sorted(conns.items(), key=lambda x: -x[1])[:3]
        facts.append("Programs with the most connections: " + ", ".join(f"{n} ({c})" for n, c in top))
    # devices on the network (from the dashboard collector, if installed)
    try:
        with open(DASH_STATUS) as f:
            devs = json.load(f)["network"]["devices"]
        home = [d for d in devs if d.get("up")]
        new = [d for d in home if not d.get("known")]
        facts.append(f"{len(home)} devices are on the network" +
                     (f", including {len(new)} I don't recognize" if new else ""))
    except Exception:
        pass
    if run_speed_test:
        s = _speed_test()
        if s:
            facts.append("Speed test: " + ", ".join(
                f"{k.replace('_mbps', '').replace('_ms', '')} {v}{' ms' if k.endswith('_ms') else ' Mbps'}"
                for k, v in s.items()))
            os.makedirs(GUARD, exist_ok=True)
            with open(os.path.join(GUARD, "speed-history.jsonl"), "a") as f:
                f.write(json.dumps(dict(s, ts=int(time.time()))) + "\n")
        else:
            facts.append("The speed test couldn't reach the internet, so the internet connection itself may be down")
    facts.append("Note: a laptop can only see its own traffic. Per-device usage needs a router that reports it")
    return ". ".join(facts) + "."


def _baseline(name, current):
    """Compare a set of items with the saved baseline; save the baseline on first run."""
    os.makedirs(GUARD, exist_ok=True)
    p = os.path.join(GUARD, name + ".json")
    if not os.path.exists(p):
        with open(p, "w") as f:
            json.dump(sorted(current), f)
        return set(), True
    with open(p) as f:
        known = set(json.load(f))
    return set(current) - known, False


def security_check():
    facts = []
    # 1. listening ports open to the network (not just localhost)
    ports = set()
    for line in _sh("ss -tulnpH").splitlines():
        p = line.split()
        if len(p) >= 5 and not re.match(r"(127\.|\[::1\]|::1)", p[4]):
            prog = re.search(r'"([^"]+)"', line)
            addr = re.sub(r"\[?fe80::[^\]%]*\]?(%[^:\s]+)?", "[fe80::]", p[4])   # link-local address changes each boot
            port = addr.rsplit(":", 1)[-1]
            if p[0] == "udp" and not prog and port.isdigit() and int(port) >= 32768:
                addr = addr.rsplit(":", 1)[0] + ":random"   # system's random UDP port, new each boot
            ports.add(f"{p[0]} {addr} {prog.group(1) if prog else '?'}")
    new, first = _baseline("ports", ports)
    facts.append(f"{len(ports)} programs are listening for network connections" +
                 (" (saved as your normal baseline)" if first else
                  f"; NEW since baseline: {', '.join(sorted(new))}" if new else "; all match your normal baseline"))
    # 2. remote logins / sessions
    who = [l for l in _sh("who").splitlines() if re.search(r"\(\d+\.\d+\.\d+\.\d+\)", l)]
    facts.append(f"Remote login sessions right now: {len(who)}" + (f" -> {'; '.join(who)}" if who else ""))
    fails = _sh("journalctl --since '-24h' -q --no-pager 2>/dev/null | grep -ciE 'failed password|authentication failure'")
    if fails and fails != "0":
        facts.append(f"{fails} failed login attempts in the last 24 hours")
    # 3. startup programs (autostart + user services + cron)
    items = set()
    for d in (os.path.expanduser("~/.config/autostart"),):
        if os.path.isdir(d):
            items |= {"autostart:" + f for f in os.listdir(d)}
    items |= {"user-service:" + l.split()[0] for l in
              _sh("systemctl --user list-unit-files --state=enabled --no-legend").splitlines() if l.strip()}
    items |= {"cron:" + l for l in _sh("crontab -l 2>/dev/null").splitlines() if l.strip() and not l.startswith("#")}
    new, first = _baseline("startup", items)
    facts.append(f"{len(items)} things start automatically" +
                 (" (saved as baseline)" if first else
                  f"; NEW since baseline: {', '.join(sorted(new))}" if new else "; nothing new"))
    # 4. unknown devices on the network
    try:
        with open(DASH_STATUS) as f:
            devs = json.load(f)["network"]["devices"]
        strangers = [d for d in devs if d.get("up") and not d.get("known")]
        facts.append("Unknown devices on the network: " +
                     (", ".join(f"{d['ip']} ({d.get('mac', '?')})" for d in strangers) if strangers else "none"))
    except Exception:
        pass
    facts.append("If something looks wrong, don't delete anything yet; tell the owner first")
    return ". ".join(facts) + "."


def accept_baseline():
    """Owner confirmed the current ports/startup items are fine: make them the new normal."""
    for n in ("ports", "startup"):
        p = os.path.join(GUARD, n + ".json")
        if os.path.exists(p):
            os.remove(p)
    security_check()
    return "Done. I saved what's running now as the new normal."
