"""Local self-test: va_mcp and va_worker against fake servers on 127.0.0.1 (no real network, no real secrets)."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import config as cfg_mod, va_extras, va_mcp, va_worker

TMP = tempfile.mkdtemp()
SECRET = os.path.join(TMP, "secret"); open(SECRET, "w").write("s3cret\n")
TOKEN = os.path.join(TMP, "tok"); open(TOKEN, "w").write("tok123\n")
CALLS = []


class Mcp(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        m = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CALLS.append((m.get("method"), self.headers.get("X-Key"), self.headers.get("Mcp-Session-Id")))
        if "id" not in m:
            self.send_response(202); self.end_headers(); return
        meth = m["method"]
        res = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {}},
               "tools/list": {"tools": [{"name": "get_thing", "description": "read"}, {"name": "delete_thing", "description": "write"}]},
               "tools/call": {"content": [{"type": "text", "text": "ok:" + (m.get("params") or {}).get("name", "") + "x" * 5000}]}}[meth]
        body = {"jsonrpc": "2.0", "id": m["id"], "result": res}
        if meth == "tools/call":   # SSE-style
            data = ("event: message\ndata: %s\n\n" % json.dumps(body)).encode(); ct = "text/event-stream"
        else:
            data = json.dumps(body).encode(); ct = "application/json"
        self.send_response(200); self.send_header("Content-Type", ct)
        if meth == "initialize": self.send_header("Mcp-Session-Id", "sess1")
        self.end_headers(); self.wfile.write(data)


class Worker(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _go(self, out):
        if self.headers.get("Authorization") != "Bearer tok123":
            self.send_response(401); self.end_headers(); return
        d = json.dumps(out).encode(); self.send_response(200); self.end_headers(); self.wfile.write(d)
    def do_GET(self):
        self._go({"summary": "web ok" if "topic=web" in self.path else "status"})
    def do_POST(self):
        n = int(self.headers["Content-Length"]); b = json.loads(self.rfile.read(n))
        self._go({"answer": "echo " + b.get("q", ""), "brain": "fake"} if self.path == "/ask" else {"ok": 1})


def serve(h, port):
    s = HTTPServer(("127.0.0.1", port), h); threading.Thread(target=s.serve_forever, daemon=True).start(); return s

cfg = {"mcp_servers": [{"name": "fake", "url": "http://127.0.0.1:18765/", "enabled": True,
                        "secret_headers": {"X-Key": SECRET}, "auto_approve": ["^get_"]}],
       "edge_worker": {"enabled": True, "url": "http://127.0.0.1:18766", "token_file": TOKEN, "timeout": 3}}
cfg_mod.load_config = lambda: cfg
va_extras._PENDING_JOB = os.path.join(TMP, "pending.json")
s1, s2 = serve(Mcp, 18765), serve(Worker, 18766)
fail = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name); cond or fail.append(name)

check("mcp_servers reachable", "fake: reachable" in va_mcp.list_servers())
t = va_mcp.list_tools("fake")
check("tools/list", "get_thing" in t and "delete_thing (needs owner approval)" in t)
check("secret header sent, session id kept", ("tools/list", "s3cret", "sess1") in CALLS)
check("initialize once", sum(1 for c in CALLS if c[0] == "initialize") == 1 and any(c[0] == "notifications/initialized" for c in CALLS))
r = va_mcp.call("fake", "get_thing", "{}")
check("auto-approved call via SSE, labeled + truncated", "ok:get_thing" in r and "untrusted" in r and len(r) < 4400)
n = len(CALLS)
r = va_mcp.call("fake", "delete_thing", "{}")
check("non-approved call blocked pending code", "approve " in r and not any(c[0] == "tools/call" for c in CALLS[n:]))
code = json.load(open(va_extras._PENDING_JOB))["code"]
check("wrong code does not run", "doesn't match" in va_extras.free_job_approve("000000" if code != "000000" else "111111"))
r = va_extras.free_job_approve(code)
check("owner code runs it", "ok:delete_thing" in r)
try:
    va_mcp.call("fake", "delete_thing", "{}", confirm=lambda t, x: False); check("declined raises", False)
except va_extras.ExtraError: check("declined confirm refuses", True)
check("worker report", "web ok" in va_worker.report("web"))
check("worker ask", "echo hi" in va_worker.ask("hi"))
check("worker note", "Saved" in va_worker.note("t", "b"))
open(TOKEN, "w").write("wrong")
check("worker bad token friendly", "refused my token" in va_worker.report("web"))
s1.shutdown(); s2.shutdown()
cfg["edge_worker"]["url"] = "http://127.0.0.1:18766"
open(TOKEN, "w").write("tok123")
check("worker unreachable friendly", "couldn't reach" in va_worker.report("all"))
cfg["edge_worker"]["enabled"] = False
check("worker not configured friendly", "isn't set up" in va_worker.report("all"))
print("FAILED: %s" % fail if fail else "ALL PASSED"); sys.exit(1 if fail else 0)
