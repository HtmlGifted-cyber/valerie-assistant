"""Local self-test: the Perplexity Agent API brain against a fake server on
127.0.0.1 (no real network, no real secrets - a dummy key stands in and the
official SDK client is redirected to the fake server via brain._perplexity_client).

Covers the documented Agent API contract (docs.perplexity.ai): POST /v1/agent
(served here through the SDK's /v1/responses alias), presets vs direct models,
history replay as input message items, output_text, url_citation annotations,
the sanitized error paths, the web_answer tool, a full Ollama round trip
(the local model calls web_answer and relays the sourced answer), opt-in
sharing (skills, connectors, MCP) with the per-call approval loop, and
conversation memory via previous_response_id.
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import brain, config as cfg_mod

try:
    from perplexity import Perplexity
except ImportError:
    # The SDK needs Python 3.10+ (requirements.txt gates it); on an older
    # runtime there is nothing here to test.
    print("SKIP: perplexityai is not installed on this Python (it needs 3.10+).")
    sys.exit(0)

KEY = "dummy-key-for-test"
PORT = 18770
OLLAMA_PORT = 18771
REQUESTS = []
OLLAMA_REQUESTS = []
SERVERS = []
APPROVAL_COUNT = [0]
FAULT = [None]  # None | "401" | "429" | "429long" | "failed" | "empty" | "no-annotations" | "approval" | "approval-multi"


def _response_body(fault):
    if fault == "empty":
        text, annotations = "", []
    elif fault == "no-annotations":
        text, annotations = "The answer without sources.", []
    else:
        text = "The answer."
        annotations = [{"type": "url_citation", "url": f"https://example.com/{c}", "title": f"Example {c}"}
                       for c in "abcdefg"]
        annotations.insert(1, {"type": "url_citation", "url": "https://example.com/a", "title": "Example A duplicate"})
    return {
        "id": f"resp_{len(REQUESTS)}", "object": "response", "created_at": 1,
        "status": "failed" if fault == "failed" else ("incomplete" if fault == "incomplete" else "completed"),
        "model": "openai/gpt-6-luna",
        "output": [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": annotations}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


class Agent(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        REQUESTS.append((self.path, self.headers.get("Authorization"), body))
        fault = FAULT[0]
        if fault == "401":
            data = json.dumps({"error": {"message": "bad key"}}).encode()
            self.send_response(401); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if fault == "429":
            data = b"{}"
            self.send_response(429); self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0.1")   # keep the SDK's automatic retry fast
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if fault == "429long":
            data = b"{}"
            self.send_response(429); self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "120")  # longer than the SDK would ever honor
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if fault in ("approval", "approval-multi"):
            has_shared = any(t.get("type") in ("connector", "mcp") for t in body.get("tools") or [])
            deciding = any(i.get("type") == "mcp_approval_response" for i in body.get("input") or [])
            if has_shared and not deciding:
                data = json.dumps({
                    "id": f"resp_{len(REQUESTS)}", "object": "response", "created_at": 1,
                    "status": "completed", "model": "openai/gpt-6-luna",
                    "output": [{"type": "mcp_approval_request", "id": "appr1", "name": "search_issues",
                                "server_label": "github", "arguments": {"q": "perplexity"}}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data); return
            if fault == "approval-multi":
                APPROVAL_COUNT[0] += 1
                if APPROVAL_COUNT[0] == 1:
                    # text AND another pending approval in one response.
                    data = json.dumps({
                        "id": f"resp_{len(REQUESTS)}", "object": "response", "created_at": 1,
                        "status": "completed", "model": "openai/gpt-6-luna",
                        "output": [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "Partial answer.", "annotations": []}]},
                                   {"type": "mcp_approval_request", "id": "appr2", "name": "fetch_file",
                                    "server_label": "github", "arguments": {"path": "x"}}],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data))); self.end_headers()
                    self.wfile.write(data); return
            # decision received - fall through to the normal answer
        data = json.dumps(_response_body(fault)).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


class Ollama(BaseHTTPRequestHandler):
    """Fake local Ollama: turn 1 calls the web_answer tool, turn 2 relays
    the sourced Perplexity answer."""
    def log_message(self, *a): pass
    def do_GET(self):
        data = json.dumps({"models": [{"name": "qwen2.5:3b"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        OLLAMA_REQUESTS.append(body)
        if len(OLLAMA_REQUESTS) == 1:
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"function": {"name": "web_answer",
                                                "arguments": {"query": "who won the world series?"}}}]}
        else:
            msg = {"role": "assistant", "content": "The Yankees won, per the sources."}
        out = (json.dumps({"message": msg, "done": True}) + "\n").encode()
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)


def serve():
    for srv in (HTTPServer(("127.0.0.1", PORT), Agent), HTTPServer(("127.0.0.1", OLLAMA_PORT), Ollama)):
        SERVERS.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()


# Keep the production client factory under test (it sets max_retries=0 so a
# 429 is never retried prematurely); only the SDK constructor is wrapped to
# inject the local URL - no public base_url config exists.
_REAL_INIT = Perplexity.__init__


def _local_init(self, *a, **kw):
    kw["base_url"] = f"http://127.0.0.1:{PORT}"
    _REAL_INIT(self, *a, **kw)


Perplexity.__init__ = _local_init

CFG = {"assistant": {"persona": "You are Valerie, a helpful local assistant.",
                     "pinned_files": [], "history_turns": 2},
       "perplexity": {"preset": "low", "model": ""}}
HISTORY = [{"role": "user", "content": "what is rust?"},
           {"role": "assistant", "content": "a systems programming language."}]

s = None
serve()
fail = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name); cond or fail.append(name)

# --- missing key: environment only, a config api_key must NOT be used -------
os.environ.pop("PERPLEXITY_API_KEY", None)
cfg_with_key = {"assistant": CFG["assistant"], "perplexity": {"preset": "low", "model": "", "api_key": "should-be-ignored"}}
r = brain.run_agent_loop("perplexity", "hi", cfg_with_key)
check("missing key friendly, points to console + env var",
      "No Perplexity API key" in r and "console.perplexity.ai" in r and "PERPLEXITY_API_KEY" in r)
check("config api_key ignored (environment only)", "should-be-ignored" not in json.dumps(REQUESTS))

os.environ["PERPLEXITY_API_KEY"] = KEY

# --- preset path --------------------------------------------------------------
r = brain.run_agent_loop("perplexity", "what is serverless?", CFG, history=HISTORY)
path, auth, body = REQUESTS[-1]
check("preset request: 200, answer text from output_text", r.startswith("The answer."))
check("request hits the SDK's /v1/responses alias of POST /v1/agent", path == "/v1/responses")
check("preset sent, no model", body.get("preset") == "low" and "model" not in body)
check("bearer auth from env, key never printed", auth == "Bearer " + KEY and KEY not in r)
check("instructions carry Valerie's persona", "Valerie" in (body.get("instructions") or ""))
check("history replayed as input message items + new user turn",
      [i["role"] for i in body["input"]] == ["user", "assistant", "user"]
      and all(i["type"] == "message" for i in body["input"])
      and body["input"][0]["content"] == "what is rust?"
      and "serverless" in body["input"][-1]["content"])
check("cited urls appended, deduplicated, capped at five",
      "Sources:" in r and r.count("https://example.com/a") == 1
      and sum(r.count(f"https://example.com/{c}") for c in "abcdefg") == 5)

# --- direct-model paths --------------------------------------------------------
CFG["perplexity"]["model"] = "openai/gpt-5.6-sol"
brain.run_agent_loop("perplexity", "hi", CFG)
_, _, body = REQUESTS[-1]
check("direct model: model + web_search tool, preset dropped",
      body.get("model") == "openai/gpt-5.6-sol" and body.get("tools") == [{"type": "web_search"}] and "preset" not in body)

CFG["perplexity"]["model"] = "anthropic/claude-opus-5-5"
brain.run_agent_loop("perplexity", "hi", CFG)
_, _, body = REQUESTS[-1]
check("anthropic model: max_output_tokens included (documented requirement)",
      body.get("max_output_tokens") == 8192)
CFG["perplexity"]["model"] = ""

# --- response variants ---------------------------------------------------------
FAULT[0] = "no-annotations"
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("no annotations -> no Sources section", r == "The answer without sources.")
FAULT[0] = "empty"
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("empty output_text -> No response.", r == "No response.")
FAULT[0] = "failed"
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("failed status -> friendly message, no raw error body", r == "Perplexity couldn't complete that request.")

# --- error paths (SDK maps HTTP codes to typed exceptions) --------------------
FAULT[0] = "401"
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("401 -> authentication message, no key in message", "rejected the API key" in r and KEY not in r)
FAULT[0] = "429"
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("429 with fractional Retry-After -> generic rate-limit message",
      "rate-limiting" in r and "in a minute" in r)

FAULT[0] = "429long"
n_before = len(REQUESTS)
r = brain.run_agent_loop("perplexity", "hi", CFG)
check("429 with long Retry-After: exactly one request, interval relayed",
      len(REQUESTS) - n_before == 1 and "in about 120 seconds" in r)
FAULT[0] = None

# Integer Retry-After from the server must be reflected in the message.
import perplexity as _pplx2

class _FakeResp:
    status_code = 429
    headers = {"Retry-After": "30"}
    class request: pass

_rl = _pplx2.RateLimitError("rl", response=_FakeResp(), body=None)


def _raise(key):
    raise _rl


real_client = brain._perplexity_client
brain._perplexity_client = _raise
try:
    r = brain.run_agent_loop("perplexity", "hi", CFG)
finally:
    brain._perplexity_client = real_client
check("429 with integer Retry-After: 30 -> 'in about 30 seconds'", "in about 30 seconds" in r)
FAULT[0] = None

# --- web_answer tool (local brain delegates web questions) ---------------------
import tools as tools_mod

TOOL_CFG = {"assistant": CFG["assistant"], "perplexity": {"preset": "low", "model": ""},
            "enabled_tools": {"web_answer": True}}
check("web_answer advertised to the model when enabled",
      any(t["name"] == "web_answer" for t in tools_mod.enabled_schemas(TOOL_CFG)))
TOOL_CFG["enabled_tools"]["web_answer"] = False
check("web_answer hidden when disabled",
      all(t["name"] != "web_answer" for t in tools_mod.enabled_schemas(TOOL_CFG)))
TOOL_CFG["enabled_tools"]["web_answer"] = True

# execute_tool loads config itself; point it at the test config and keep the
# activity log out of the real one.
real_load, real_log = cfg_mod.load_config, cfg_mod.log_activity


def _tool_call():
    cfg_mod.load_config = lambda: TOOL_CFG
    cfg_mod.log_activity = lambda *a, **k: None
    try:
        return tools_mod.execute_tool("web_answer", {"query": "who won the world series?"})
    finally:
        cfg_mod.load_config, cfg_mod.log_activity = real_load, real_log


r = _tool_call()
_, _, body = REQUESTS[-1]
check("web_answer request: single user message, no history replay",
      [i["role"] for i in body["input"]] == ["user"]
      and body["input"][0]["content"] == "who won the world series?")
check("web_answer request: compact voice-assistant instructions, preset",
      "voice assistant" in body["instructions"] and body.get("preset") == "low")
check("web_answer result: answer + sources for the calling model",
      r.startswith("The answer.") and "Sources:" in r)

os.environ.pop("PERPLEXITY_API_KEY", None)
n_before = len(REQUESTS)
r = _tool_call()
os.environ["PERPLEXITY_API_KEY"] = KEY
check("web_answer without key: guidance in the result, no API call made",
      "console.perplexity.ai" in r and len(REQUESTS) == n_before)

TOOL_CFG["enabled_tools"]["web_answer"] = False
check("web_answer schema hidden when disabled",
      all(t["name"] != "web_answer" for t in tools_mod.enabled_schemas(TOOL_CFG)))
n_before = len(REQUESTS)
r = _tool_call()
check("web_answer refused when disabled", r.startswith("Tool denied"))
check("no Perplexity request when disabled", len(REQUESTS) == n_before)
TOOL_CFG["enabled_tools"].pop("web_answer")  # absent flag: opt-in default must hold
check("web_answer schema hidden when flag absent (opt-in default)",
      all(t["name"] != "web_answer" for t in tools_mod.enabled_schemas(TOOL_CFG)))
n_before = len(REQUESTS)
r = _tool_call()
check("web_answer refused when flag absent (opt-in default)", r.startswith("Tool denied"))
check("no Perplexity request when flag absent", len(REQUESTS) == n_before)
TOOL_CFG["enabled_tools"]["web_answer"] = True

# --- full Ollama round trip: local model -> web_answer -> sourced reply --------
OLLAMA_CFG = {"assistant": CFG["assistant"], "perplexity": {"preset": "low", "model": ""},
              "enabled_tools": {"web_answer": True},
              "ollama": {"host": f"http://127.0.0.1:{OLLAMA_PORT}", "model": "qwen2.5:3b",
                         "timeout": 30}}
OLLAMA_REQUESTS.clear()
chat = []
cfg_mod.load_config = lambda: OLLAMA_CFG
cfg_mod.log_activity = lambda *a, **k: None
try:
    r = brain.call_ollama("who won the world series?", OLLAMA_CFG,
                          update_chat=lambda who, txt, **k: chat.append(txt))
finally:
    cfg_mod.load_config, cfg_mod.log_activity = real_load, real_log
check("ollama round trip: model receives the web_answer schema",
      any(t.get("function", {}).get("name") == "web_answer" for t in OLLAMA_REQUESTS[0].get("tools", [])))
check("ollama round trip: tool result carries the Perplexity answer + sources",
      any(m.get("role") == "tool" and "The answer." in m.get("content", "")
          and "https://example.com/a" in m.get("content", "")
          for m in OLLAMA_REQUESTS[1]["messages"]))
check("ollama round trip: final relayed reply after the tool call",
      r == "The Yankees won, per the sources." and "(using web_answer...)" in chat)

# --- sharing: skills, connectors, MCP (all opt-in) -----------------------------
import tempfile


def _skill_dir():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "test-skill.md"), "w") as f:
        f.write("name: test-skill\ndescription: Test skill for the fake server.\n---\nWrite a limerick about {input}.\n")
    return d


real_skills_dir = cfg_mod.SKILLS_DIR
cfg_mod.SKILLS_DIR = _skill_dir()
try:
    SKILL_CFG = {"assistant": CFG["assistant"],
                 "perplexity": {"preset": "low", "model": "", "skills": ["test-skill"]}}
    brain.run_agent_loop("perplexity", "hi", SKILL_CFG)
    _, _, body = REQUESTS[-1]
    check("skills shared as inline Agent API skills",
          body.get("skills") == [{"type": "inline", "name": "test-skill",
                                  "description": "Test skill for the fake server.",
                                  "instructions": "Write a limerick about {input}."}])
    check("max_steps defaults on when skills are shared", body.get("max_steps") == 12)

    SKILL_CFG["perplexity"]["skills"] = ["missing-skill"]
    r = brain.run_agent_loop("perplexity", "hi", SKILL_CFG)
    check("missing skill name -> friendly config error",
          "doesn't exist in Valerie's skills folder" in r and "missing-skill" in r)

    SKILL_CFG["perplexity"]["skills"] = ["s%d" % i for i in range(17)]
    r = brain.run_agent_loop("perplexity", "hi", SKILL_CFG)
    check("more than 16 skills -> friendly config error", "at most 16 skills" in r)

    with open(os.path.join(cfg_mod.SKILLS_DIR, "Foo_bar.md"), "w") as f:
        f.write("name: Foo_bar\ndescription: Collides after normalization.\n---\nOne.\n")
    with open(os.path.join(cfg_mod.SKILLS_DIR, "foo-bar.md"), "w") as f:
        f.write("name: foo-bar\ndescription: Collides after normalization.\n---\nTwo.\n")
    SKILL_CFG["perplexity"]["skills"] = ["Foo_bar", "foo-bar"]
    r = brain.run_agent_loop("perplexity", "hi", SKILL_CFG)
    check("skill names colliding after normalization -> friendly error",
          "normalize to the same name" in r and "Foo_bar" in r and "foo-bar" in r)
finally:
    cfg_mod.SKILLS_DIR = real_skills_dir

CONN_CFG = {"assistant": CFG["assistant"],
            "perplexity": {"preset": "low", "model": "",
                           "connectors": [{"id": "connector_github", "label": "github"}]}}
brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, body = REQUESTS[-1]
check("connector forwarded with require_approval always (web_search kept)",
      body.get("tools") == [{"type": "web_search"},
                            {"type": "connector", "id": "connector_github", "server_label": "github",
                             "require_approval": "always"}])
CONN_CFG["perplexity"]["model"] = "openai/gpt-5.6-sol"
brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, body = REQUESTS[-1]
check("direct model: web_search + connector, preset dropped",
      body.get("tools") == [{"type": "web_search"},
                            {"type": "connector", "id": "connector_github", "server_label": "github",
                             "require_approval": "always"}] and "preset" not in body)
CONN_CFG["perplexity"]["model"] = ""

with tempfile.NamedTemporaryFile("w", suffix=".hdr", delete=False) as _f:
    _f.write("topsecrettoken\n")
    secret_path = _f.name
MCP_CFG = {"assistant": CFG["assistant"],
           "perplexity": {"preset": "low", "model": "", "mcp": ["cloud-tools"]},
           "mcp_servers": [{"name": "cloud-tools", "url": "https://mcp.example.com/mcp",
                           "enabled": True, "allow_tools": ["search", "fetch"],
                           "headers": {"X-Lit": "1"}, "secret_headers": {"X-Secret": secret_path}}]}
brain.run_agent_loop("perplexity", "hi", MCP_CFG)
_, _, body = REQUESTS[-1]
check("mcp server forwarded: https url, allowlist, secret read at call time",
      body.get("tools") == [{"type": "web_search"},
                            {"type": "mcp", "server_label": "cloud-tools", "server_url": "https://mcp.example.com/mcp",
                             "require_approval": "always", "allowed_tools": ["search", "fetch"],
                             "headers": {"X-Lit": "1", "X-Secret": "topsecrettoken"}}])
MCP_CFG["mcp_servers"][0]["url"] = "http://127.0.0.1:9999/mcp"
r = brain.run_agent_loop("perplexity", "hi", MCP_CFG)
check("local/LAN mcp url refused", "public HTTPS URL" in r)
MCP_CFG["mcp_servers"][0]["url"] = "https://[::1]/mcp"
r = brain.run_agent_loop("perplexity", "hi", MCP_CFG)
check("IPv6 loopback mcp url refused", "public HTTPS URL" in r)
MCP_CFG["mcp_servers"][0]["url"] = "https://[::1/mcp"   # malformed bracketed IPv6
r = brain.run_agent_loop("perplexity", "hi", MCP_CFG)
check("malformed bracketed mcp url refused, not crashed", "public HTTPS URL" in r)
MCP_CFG["mcp_servers"][0]["url"] = "https://172.100.1.2/mcp"   # 172.100.x is public space
brain.run_agent_loop("perplexity", "hi", MCP_CFG)
_, _, body = REQUESTS[-1]
check("public 172.x address allowed (only private ranges refused)",
      any(t.get("server_url") == "https://172.100.1.2/mcp" for t in body.get("tools") or []))
MCP_CFG["mcp_servers"][0]["url"] = "https://mcp.example.com/mcp"
MCP_CFG["mcp_servers"][0]["enabled"] = False
brain.run_agent_loop("perplexity", "hi", MCP_CFG)
_, _, body = REQUESTS[-1]
check("mcp server disabled in Valerie's config stays off for Perplexity",
      "tools" not in body)
MCP_CFG["mcp_servers"][0]["enabled"] = True
MCP_CFG["perplexity"]["mcp"] = ["not-configured"]
r = brain.run_agent_loop("perplexity", "hi", MCP_CFG)
check("unknown mcp name -> friendly error", "isn't in Valerie's mcp_servers" in r)
os.unlink(secret_path)

# --- per-call approval loop for connector/MCP tools ------------------------------
FAULT[0] = "approval"
n_before = len(REQUESTS)
r = brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, first = REQUESTS[-2]
_, _, cont = REQUESTS[-1]
check("approval flow: continuation resends tools unchanged + previous_response_id",
      cont.get("tools") == first.get("tools")
      and cont.get("previous_response_id") == f"resp_{n_before + 1}")
check("headless (no confirm callback) denies with a reason",
      cont["input"] == [{"type": "mcp_approval_response", "approval_request_id": "appr1",
                         "approve": False, "reason": "No one is available to approve this call."}])
check("final answer still returned after a denial", r.startswith("The answer."))

brain.CONFIRM = lambda title, text: True   # owner approves everything
r = brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, cont = REQUESTS[-1]
check("owner approval sends approve=True, no reason",
      cont["input"][0]["approve"] is True and "reason" not in cont["input"][0])
brain.CONFIRM = None

# web_answer approvals flow through tools.CONFIRM
TOOL_CFG["perplexity"]["connectors"] = [{"id": "connector_slack", "label": "slack"}]
tools_mod.CONFIRM = lambda t, b: True
r = _tool_call()
_, _, cont = REQUESTS[-1]
check("web_answer tool: approval decisions flow through tools.CONFIRM",
      cont["input"][0]["approve"] is True and r.startswith("The answer."))
tools_mod.CONFIRM = None
TOOL_CFG["perplexity"].pop("connectors")

# connector allowlist is forwarded and resent on the continuation
CONN_CFG["perplexity"]["connectors"] = [{"id": "connector_slack", "label": "slack",
                                          "allowed_tools": ["slack_post_message"]}]
brain.CONFIRM = lambda title, text: True
brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, cont = REQUESTS[-1]
check("connector allowlist travels inside the connector entry and is resent",
      any(t.get("type") == "connector" and t.get("id") == "connector_slack"
          and t.get("allowed_tools") == ["slack_post_message"]
          for t in (cont.get("tools") or [])))

# anthropic approval continuation keeps its required max_output_tokens
CONN_CFG["perplexity"]["model"] = "anthropic/claude-opus-5-5"
brain.run_agent_loop("perplexity", "hi", CONN_CFG)
_, _, cont = REQUESTS[-1]
check("anthropic approval continuation retains max_output_tokens",
      cont.get("max_output_tokens") == 8192 and cont.get("model") == "anthropic/claude-opus-5-5")
CONN_CFG["perplexity"]["model"] = ""
CONN_CFG["perplexity"]["connectors"] = [{"id": "connector_github", "label": "github"}]
brain.CONFIRM = None

# repeated approvals: text + another pending request keeps the loop going
APPROVAL_COUNT[0] = 0
FAULT[0] = "approval-multi"
state_a = {}
r = brain.run_agent_loop("perplexity", "hi", CONN_CFG, conv_state=state_a)
check("multi-round approvals: two decisions, then the final answer",
      r.startswith("The answer.") and r != "Partial answer."
      and sum(1 for _, _, b in REQUESTS[-3:] if any(i.get("type") == "mcp_approval_response" for i in b["input"])) == 2)
check("multi-round approvals: run completes with a reusable response id",
      state_a.get("resp_id") is not None)

# the approval loop bounds decision ROUNDS, not requests: one round then a
# completed answer succeeds even at the limit.
real_max_turns = brain.MAX_TOOL_TURNS
brain.MAX_TOOL_TURNS = 1
FAULT[0] = "approval"
state_lim = {}
r = brain.run_agent_loop("perplexity", "hi", CONN_CFG, conv_state=state_lim)
check("single approval round within the limit -> success",
      r.startswith("The answer.") and state_lim.get("resp_id") is not None)
FAULT[0] = "approval-multi"
APPROVAL_COUNT[0] = 0
r = brain.run_agent_loop("perplexity", "hi", CONN_CFG)
check("second approval round past the limit -> explicit message, no conversation id",
      "Too many approval rounds" in r)
brain.MAX_TOOL_TURNS = real_max_turns
FAULT[0] = None

# --- conversation memory: previous_response_id (caller-owned state) -------------
state = {}
hist = list(HISTORY)
r1 = brain.run_agent_loop("perplexity", "first question", CFG, history=hist, conv_state=state)
_, _, first_body = REQUESTS[-1]
first_id = f"resp_{len(REQUESTS)}"
check("first call: full replay, no previous_response_id",
      "previous_response_id" not in first_body and len(first_body["input"]) == 3)
hist += [{"role": "user", "content": "first question"}, {"role": "assistant", "content": r1}]
brain.run_agent_loop("perplexity", "second question", CFG, history=hist, conv_state=state)
_, _, second = REQUESTS[-1]
check("continuation: previous_response_id sent, only the new turn",
      second.get("previous_response_id") == first_id and len(second["input"]) == 1)

bad_state = {"resp_id": "resp_x", "tail": [("user", "a different conversation")]}
brain.run_agent_loop("perplexity", "hi", CFG, history=hist, conv_state=bad_state)
_, _, body = REQUESTS[-1]
check("state mismatch -> full replay without previous_response_id",
      "previous_response_id" not in body and len(body["input"]) == 5)

FAULT[0] = "429"
failing = {"resp_id": "resp_y", "tail": brain._history_snapshot(hist, CFG)}
brain.run_agent_loop("perplexity", "hi", CFG, history=hist, conv_state=failing)
check("failed run clears the conversation state (next turn replays)", failing == {})
FAULT[0] = "incomplete"
incomplete = {"resp_id": "resp_z", "tail": brain._history_snapshot(hist, CFG)}
r = brain.run_agent_loop("perplexity", "hi", CFG, history=hist, conv_state=incomplete)
check("incomplete response: text returned but its id is not reused",
      r.startswith("The answer.") and incomplete == {})
FAULT[0] = None

sa, sb = {}, {}
hist_b = list(HISTORY)
brain.run_agent_loop("perplexity", "q1", CFG, history=hist_b, conv_state=sa)
brain.run_agent_loop("perplexity", "q2", CFG, history=hist_b, conv_state=sb)
check("separate conversations keep separate response ids", sa["resp_id"] != sb["resp_id"])

# a turn answered by a DIFFERENT provider between two Perplexity turns must
# not be skipped: the exact snapshot no longer matches, so the brain replays.
hist_c = hist + [{"role": "user", "content": "via ollama"},
                 {"role": "assistant", "content": "an ollama answer"}]
brain.run_agent_loop("perplexity", "next", CFG, history=hist_c, conv_state=state)
_, _, body = REQUESTS[-1]
check("mixed-provider turns are never skipped (exact match required)",
      "previous_response_id" not in body and len(body["input"]) == 5
      and body["input"][2]["content"] == "via ollama")

# --- default config wiring -----------------------------------------------------
merged = cfg_mod.DEFAULT_CONFIG["perplexity"]
check("DEFAULT_CONFIG has perplexity defaults, no api_key field",
      merged == {"preset": "low", "model": "", "skills": [], "connectors": [],
                 "mcp": [], "max_steps": 0})

for srv in SERVERS:
    srv.shutdown()
print("FAILED: %s" % fail if fail else "ALL PASSED"); sys.exit(1 if fail else 0)
