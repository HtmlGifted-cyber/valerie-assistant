"""Local self-test: the Perplexity Agent API brain against a fake server on
127.0.0.1 (no real network, no real secrets - a dummy key stands in and the
official SDK client is redirected to the fake server via brain._perplexity_client).

Covers the documented Agent API contract (docs.perplexity.ai): POST /v1/agent
(served here through the SDK's /v1/responses alias), presets vs direct models,
history replay as input message items, output_text, url_citation annotations,
the sanitized error paths, the web_answer tool, and a full Ollama round trip
(the local model calls web_answer and relays the sourced answer).
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
FAULT = [None]  # None | "401" | "429" | "429long" | "failed" | "empty" | "no-annotations"


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
        "id": "resp_test", "object": "response", "created_at": 1,
        "status": "failed" if fault == "failed" else "completed",
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

# --- default config wiring -----------------------------------------------------
merged = cfg_mod.DEFAULT_CONFIG["perplexity"]
check("DEFAULT_CONFIG has perplexity preset/model, no api_key field",
      merged == {"preset": "low", "model": ""})

for srv in SERVERS:
    srv.shutdown()
print("FAILED: %s" % fail if fail else "ALL PASSED"); sys.exit(1 if fail else 0)
