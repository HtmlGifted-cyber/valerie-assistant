"""
brain.py - swappable AI backend for Valerie.

Local-first: Ollama is the default and needs no account or key. Claude /
ChatGPT / Gemini / Perplexity (web-grounded, key from PERPLEXITY_API_KEY)
remain an opt-in upgrade with your own key.

All providers share one interface:
    run_agent_loop(provider, prompt, cfg, update_chat=None, history=None) -> final text

2.2 changes:
  - Every call now carries a system prompt (persona, today's date,
    available skills) plus the best-matching knowledge-base passages
    for the question, so answers are grounded in the owner's own docs.
  - Conversation memory: the last few exchanges are sent as history.
  - Ollama timeouts raised (CPU inference can take minutes), context
    window and keep-alive configurable, and the model is told which
    tools are enabled only.
"""

import datetime
import json as _json
import os
import re
import shutil
import subprocess
import time

import requests

import config as cfg_mod
import knowledge
from tools import enabled_schemas, execute_tool

# --- cancellation -----------------------------------------------------------
# cancel() is called from the GUI's STOP button. It sets the flag and closes
# the live HTTP stream, which makes Ollama abort the generation (frees the CPU).
import threading as _threading
CANCEL = _threading.Event()
_ACTIVE = {"resp": None}

# Set by the GUI (like tools.CONFIRM): approvals for Perplexity connector/MCP
# calls route through it. None (headless scheduler) means requests are DENIED.
CONFIRM = None


class Cancelled(Exception):
    pass


def cancel():
    CANCEL.set()
    r = _ACTIVE.get("resp")
    if r is not None:
        # Shut the socket down first: a plain close() doesn't interrupt a read
        # that is blocked while the model is still processing the prompt.
        import socket
        for path in (("raw", "_connection", "sock"), ("raw", "_fp", "fp", "raw", "_sock")):
            obj = r
            try:
                for attr in path:
                    obj = getattr(obj, attr)
                obj.shutdown(socket.SHUT_RDWR)
                break
            except Exception:
                continue
        try:
            r.close()
        except Exception:
            pass


def _check():
    if CANCEL.is_set():
        raise Cancelled()


MAX_TOOL_TURNS = 8  # a full VA check-in uses several tools


def _builtin_rules():
    """Rules that ship with Valerie and cannot be switched off from config (see va_skillsmp.PINNED_RULE)."""
    try:
        import va_skillsmp
        return va_skillsmp.PINNED_RULE
    except Exception:
        return ""


def build_system_prompt(cfg, prompt=None):
    """Static on purpose: Ollama caches the processed prompt prefix
    (system prompt + tool list + history), so anything that changes per
    message (date, knowledge passages) goes into the user turn instead.
    On this CPU-only laptop that cuts each reply from minutes to seconds
    of prompt processing."""
    a = cfg.get("assistant", {})
    pinned = []
    for path in a.get("pinned_files", []):
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                pinned.append(f.read().strip())
        except OSError:
            pass
    import workspaces
    owner = workspaces.owner(cfg) or "the owner"
    persona = a.get("persona", "You are Valerie, a helpful local assistant.").replace("{owner}", owner)
    summary = workspaces.persona_summary(cfg)
    return "\n\n".join([
        persona + (" " + summary if summary and summary not in persona else ""),
        *(["Core facts you must always follow:\n\n" + "\n\n".join(pinned)] if pinned else []),
        _builtin_rules(),
        "Answer in plain text (no markdown tables); replies may be read aloud, so keep them short unless asked for detail.",
        "Each user message may start with a [Context] block (today's date and passages from the owner's knowledge base). "
        "Use it when relevant and cite the [title]; ignore it when it isn't.",
    ])


def build_user_turn(cfg, prompt, ecosystem=None):
    parts = [f"Today: {datetime.datetime.now().strftime('%A, %B %d, %Y')}"]
    import workspaces
    if ecosystem in workspaces.all_ids(cfg):
        parts.append(workspaces.ai_line(cfg, ecosystem))
    try:
        kb = knowledge.context_for(prompt, cfg, ecosystem=ecosystem)
    except Exception:
        kb = ""
    if kb:
        parts.append(kb)
    return "[Context]\n" + "\n\n".join(parts) + "\n[/Context]\n\n" + prompt


def _history_messages(history, cfg):
    turns = cfg.get("assistant", {}).get("history_turns", 6)
    return list(history or [])[-turns * 2:]


# ------------------------------------------------------------------ Ollama

def _ollama_tools(cfg):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in enabled_schemas(cfg)]


def _ollama_reachable(host):
    try:
        return requests.get(f"{host}/api/tags", timeout=2).status_code == 200
    except requests.RequestException:
        return False


def _ollama_has_model(host, model):
    try:
        names = [m["name"] for m in requests.get(f"{host}/api/tags", timeout=3).json().get("models", [])]
    except (requests.RequestException, ValueError):
        return True  # can't tell - let the real call report the error
    base = model if ":" in model else f"{model}:latest"
    return base in names


def _try_autostart_ollama(host, update_chat=None):
    if shutil.which("ollama") is None:
        return False
    if update_chat:
        update_chat("Valerie", "Ollama isn't running - starting it now, one moment...")
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    for _ in range(12):
        time.sleep(0.5)
        if _ollama_reachable(host):
            return True
    return False


def warm_up(cfg=None):
    """Pre-read the fixed part of the prompt (instructions + tool list) so
    Ollama caches it; the first real question then only processes the new
    words. Run in the background when the GUI opens."""
    cfg = cfg or cfg_mod.load_config()
    if cfg.get("active_provider", "ollama") != "ollama":
        return
    o = cfg.get("ollama", {})
    host = o.get("host", "http://127.0.0.1:11434")
    if not _ollama_reachable(host):
        return
    body = {"model": o.get("model", "qwen2.5:3b"), "stream": False,
            "messages": [{"role": "system", "content": build_system_prompt(cfg)},
                         {"role": "user", "content": "hi"}],
            "options": {"num_ctx": o.get("num_ctx", 6144), "num_predict": 1},
            "keep_alive": o.get("keep_alive", "2h")}
    tools = _ollama_tools(cfg)
    if tools:
        body["tools"] = tools
    try:
        requests.post(f"{host}/api/chat", json=body, timeout=1800)
    except requests.RequestException:
        pass


def call_ollama(prompt, cfg, update_chat=None, history=None, ecosystem=None):
    o = cfg.get("ollama", {})
    host = o.get("host", "http://127.0.0.1:11434")
    model = o.get("model", "qwen2.5:3b")
    timeout = max(int(o.get("timeout", 900)), 900)
    options = {"num_ctx": o.get("num_ctx", 6144)}

    if not _ollama_reachable(host) and not _try_autostart_ollama(host, update_chat):
        return "Ollama is offline. Run 'systemctl start ollama' (or 'ollama serve') in a terminal."
    if not _ollama_has_model(host, model):
        return f"The model '{model}' isn't downloaded yet. Run: ollama pull {model}"

    messages = [{"role": "system", "content": build_system_prompt(cfg, prompt)}]
    messages += _history_messages(history, cfg)
    messages.append({"role": "user", "content": build_user_turn(cfg, prompt, ecosystem)})
    tools = _ollama_tools(cfg)

    CANCEL.clear()
    try:
        for _ in range(MAX_TOOL_TURNS):
            _check()
            body = {"model": model, "messages": messages, "stream": True,
                    "options": options, "keep_alive": o.get("keep_alive", "2h")}
            if tools:
                body["tools"] = tools
            r = requests.post(f"{host}/api/chat", json=body, timeout=timeout, stream=True)
            _ACTIVE["resp"] = r
            try:
                if r.status_code == 400 and tools and "does not support tools" in r.text:
                    tools = []  # older model - retry this turn as plain chat
                    continue
                if r.status_code != 200:
                    return f"Ollama returned HTTP {r.status_code}: {r.text[:200]}"
                content, calls = [], []
                for line in r.iter_lines():
                    _check()
                    if not line:
                        continue
                    chunk = _json.loads(line)
                    m = chunk.get("message") or {}
                    if m.get("content"):
                        content.append(m["content"])
                    if m.get("tool_calls"):
                        calls.extend(m["tool_calls"])
                    if chunk.get("done"):
                        break
            finally:
                _ACTIVE["resp"] = None
                r.close()
            _check()
            msg = {"role": "assistant", "content": "".join(content)}
            if not calls:
                return msg["content"].strip() or "No response."
            msg["tool_calls"] = calls
            messages.append(msg)
            for call in calls:
                _check()
                fn = call.get("function", {}) or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = _json.loads(args or "{}")
                    except ValueError:
                        args = {}
                if update_chat:
                    update_chat("Valerie", f"(using {fn.get('name')}...)", speak=False)
                result = execute_tool(fn.get("name"), args, update_chat)
                messages.append({"role": "tool", "content": result})
        return "Reached the tool-call limit for this turn."
    except Cancelled:
        return "__CANCELLED__"
    except requests.Timeout:
        return f"The local model took longer than {timeout}s. Try a shorter question, or raise ollama.timeout in config.json."
    except (requests.RequestException, ValueError, AttributeError) as e:
        if CANCEL.is_set():
            return "__CANCELLED__"
        return f"Couldn't reach Ollama: {e}"


# ------------------------------------------------------------ Cloud (opt-in)

def _run_anthropic(prompt, cfg, update_chat, history, ecosystem=None):
    try:
        import anthropic
    except ImportError:
        return "The 'anthropic' package isn't installed in Valerie's venv."
    key = cfg_mod.get_api_key(cfg, "anthropic")
    if not key:
        return "No Anthropic API key configured. Add one in Valerie's Settings."

    client = anthropic.Anthropic(api_key=key)
    a = cfg.get("anthropic", {})
    model = a.get("model", "claude-opus-5-5")
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in enabled_schemas(cfg)]
    system = build_system_prompt(cfg)
    messages = _history_messages(history, cfg) + [{"role": "user", "content": build_user_turn(cfg, prompt, ecosystem)}]
    # Passed raw so the py3.8-pinned SDK needn't know these params: auto prompt
    # caching (system prompt is static), effort, and server-side refusal fallback.
    extra = {"cache_control": {"type": "ephemeral"},
             "output_config": {"effort": a.get("effort", "medium")},
             "fallbacks": "default"}
    headers = {"anthropic-beta": "server-side-fallback-2026-07-01"}

    for _ in range(MAX_TOOL_TURNS):
        resp = client.messages.create(model=model, max_tokens=16000, system=system, tools=tools, messages=messages,
                                      extra_body=extra, extra_headers=headers)
        if resp.stop_reason == "refusal":
            return "Claude declined that request."
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text") or "No response."
        messages.append({"role": "assistant", "content": resp.content})
        results = [{"type": "tool_result", "tool_use_id": tu.id, "content": execute_tool(tu.name, tu.input, update_chat)}
                   for tu in tool_uses]
        messages.append({"role": "user", "content": results})
    return "Reached the tool-call limit for this turn."


def _run_openai(prompt, cfg, update_chat, history, ecosystem=None):
    try:
        import openai
    except ImportError:
        return "The 'openai' package isn't installed in Valerie's venv."
    key = cfg_mod.get_api_key(cfg, "openai")
    if not key:
        return "No OpenAI API key configured. Add one in Valerie's Settings."

    client = openai.OpenAI(api_key=key)
    model = cfg.get("openai", {}).get("model", "gpt-4o")
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
             for t in enabled_schemas(cfg)]
    messages = [{"role": "system", "content": build_system_prompt(cfg)}]
    messages += _history_messages(history, cfg) + [{"role": "user", "content": build_user_turn(cfg, prompt, ecosystem)}]

    for _ in range(MAX_TOOL_TURNS):
        resp = client.chat.completions.create(model=model, tools=tools, messages=messages)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "No response."
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            args = _json.loads(call.function.arguments or "{}")
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": execute_tool(call.function.name, args, update_chat)})
    return "Reached the tool-call limit for this turn."


def _run_gemini(prompt, cfg, update_chat, history, ecosystem=None):
    try:
        import google.generativeai as genai
    except ImportError:
        return "The 'google-generativeai' package isn't installed in Valerie's venv."
    key = cfg_mod.get_api_key(cfg, "gemini")
    if not key:
        return "No Gemini API key configured. Add one in Valerie's Settings."

    genai.configure(api_key=key)
    decls = [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in enabled_schemas(cfg)]
    model = genai.GenerativeModel(cfg.get("gemini", {}).get("model", "gemini-2.0-flash"),
                                  tools=[{"function_declarations": decls}],
                                  system_instruction=build_system_prompt(cfg))
    hist = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [m["content"]]}
            for m in _history_messages(history, cfg)]
    chat = model.start_chat(history=hist)

    message = build_user_turn(cfg, prompt, ecosystem)
    for _ in range(MAX_TOOL_TURNS):
        resp = chat.send_message(message)
        calls = [p.function_call for p in resp.candidates[0].content.parts if getattr(p, "function_call", None)]
        if not calls:
            return resp.text or "No response."
        message = [{"function_response": {"name": c.name, "response": {"result": execute_tool(c.name, dict(c.args or {}), update_chat)}}}
                   for c in calls]
    return "Reached the tool-call limit for this turn."


def _perplexity_client(key):
    """Separate function so tests/test_perplexity.py can point the official
    SDK at a local fake server instead of the real API. Automatic retries
    are off on purpose: the SDK only honors Retry-After up to 60s and would
    otherwise retry prematurely on longer server-requested waits, so the
    429 handler below reports the server's own interval instead."""
    from perplexity import Perplexity
    return Perplexity(api_key=key, max_retries=0)


def _perplexity_skills(cfg):
    """Valerie skills chosen by name in cfg['perplexity']['skills'], sent as
    Agent API inline skills. Only the markdown instructions travel - local
    scripts, files and plugins never leave the machine. Returns (skills,
    error): a non-empty error means the config needs fixing."""
    names = cfg.get("perplexity", {}).get("skills") or []
    if not names:
        return [], None
    if len(set(names)) > 16:
        return [], "Perplexity carries at most 16 skills per request - trim the 'skills' list."
    import skills as skills_mod
    try:
        have = skills_mod.load_skills()
    except Exception:  # a broken skills folder shouldn't break the brain
        have = {}
    out, seen, seen_safe = [], set(), {}
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        sk = have.get(name)
        if sk is None:
            return [], f"Perplexity skill '{name}' doesn't exist in Valerie's skills folder."
        body = sk.get("prompt") or ""
        if len(body.encode("utf-8")) > 65536:
            return [], f"Perplexity skill '{name}' is too long to send (limit 65536 bytes)."
        safe = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:64] or "skill"
        if safe in seen_safe:
            return [], (f"Perplexity skills '{seen_safe[safe]}' and '{name}' normalize to the "
                        f"same name '{safe}' - rename one of them.")
        seen_safe[safe] = name
        desc = (sk.get("description") or f"Valerie's '{name}' skill.")
        desc = desc.encode("utf-8")[:1024].decode("utf-8", "ignore")
        out.append({"type": "inline", "name": safe, "description": desc,
                    "instructions": body})
    return out, None


def _reject_non_public_https(url):
    """None when the URL is https with a non-private-looking target, else a
    short reason. String checks can't prove cloud reachability - this only
    refuses obvious private/localhost targets."""
    from urllib.parse import urlparse
    import ipaddress
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:  # e.g. a malformed bracketed IPv6 URL
        return "needs a public HTTPS URL (private/localhost targets are refused)"
    ok = (u.scheme == "https" and bool(host) and host != "localhost"
          and not host.endswith((".local", ".lan", ".home")))
    if ok:
        try:
            ip = ipaddress.ip_address(host)
            ok = not (ip.is_private or ip.is_loopback or ip.is_link_local
                      or ip.is_reserved or ip.is_multicast)
        except ValueError:  # a hostname, not an IP literal
            pass
    return None if ok else "needs a public HTTPS URL (private/localhost targets are refused)"


def _perplexity_extra_tools(cfg):
    """Opt-in tool sharing: Perplexity Project connectors by ID, and selected
    remote MCP servers from Valerie's config. Both go out with
    require_approval 'always' - the agent may propose calls, the owner decides
    (headless runs deny). Returns (tools, error)."""
    p = cfg.get("perplexity", {})
    out = []
    for c in p.get("connectors") or []:
        cid = (c.get("id") or "").strip()
        if not cid:
            return [], "Perplexity connector entries need an 'id' from the console.perplexity.ai Project connectors page."
        entry = {"type": "connector", "id": cid,
                 "server_label": (c.get("label") or cid), "require_approval": "always"}
        if c.get("allowed_tools"):  # exact-name allowlist, per the API reference
            entry["allowed_tools"] = list(c["allowed_tools"])
        out.append(entry)
    for name in p.get("mcp") or []:
        server = next((s for s in cfg.get("mcp_servers") or [] if s.get("name") == name), None)
        if server is None:
            return [], f"Perplexity MCP server '{name}' isn't in Valerie's mcp_servers."
        if not server.get("enabled", True):
            continue  # off in Valerie stays off for Perplexity
        bad = _reject_non_public_https((server.get("url") or "").strip())
        if bad:
            return [], f"Perplexity MCP server '{name}' {bad}."
        entry = {"type": "mcp", "server_label": name, "server_url": server["url"].strip(),
                 "require_approval": "always"}
        if server.get("allow_tools"):
            entry["allowed_tools"] = list(server["allow_tools"])
        headers = dict(server.get("headers") or {})
        for h, path in (server.get("secret_headers") or {}).items():
            try:
                with open(os.path.expanduser(path), encoding="utf-8") as f:
                    headers[h] = f.read().strip()   # read at call time; never logged
            except OSError:
                return [], f"Perplexity MCP server '{name}': can't read the secret file for header '{h}'."
        if headers:
            entry["headers"] = headers
        out.append(entry)
    return out, None


def _perplexity_call(input_items, cfg, instructions, confirm=None, update_chat=None,
                     previous_response_id=None):
    """Shared core for the Perplexity brain and the web_answer tool: key
    check, request build (preset or direct model, plus opt-in shared skills
    and connector/MCP tools), the per-call approval loop for those tools,
    sanitized error mapping, and the answer plus its cited sources.
    Returns (text, resp_id); errors come back as friendly strings with a
    None id, never exceptions."""
    try:
        import perplexity as _pplx
    except ImportError:
        return "The 'perplexity' package isn't installed in Valerie's venv (it needs Python 3.10+).", None
    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        return ("No Perplexity API key found. Create one at "
                "https://console.perplexity.ai and export it as "
                "PERPLEXITY_API_KEY in your terminal.", None)

    p = cfg.get("perplexity", {})
    shared_skills, err = _perplexity_skills(cfg)
    if err:
        return err, None
    extra, err = _perplexity_extra_tools(cfg)
    if err:
        return err, None
    model = (p.get("model") or "").strip()
    preset = (p.get("preset") or "low").strip() or "low"

    kwargs = {"input": input_items, "instructions": instructions}
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    if model:
        # Direct model: web grounding is the point, so always offer search.
        # Explicit tools replace the bundled set, so web_search stays first.
        kwargs["model"] = model
        kwargs["tools"] = [{"type": "web_search"}] + extra
        if model.startswith("anthropic/"):
            # Documented requirement: Anthropic models reject a request
            # without max_output_tokens.
            kwargs["max_output_tokens"] = int(p.get("max_output_tokens", 8192))
        kwargs["max_steps"] = int(p.get("max_steps") or 12)  # tool calls + answer
    else:
        kwargs["preset"] = preset
        if extra:
            kwargs["tools"] = [{"type": "web_search"}] + extra
        if extra or shared_skills:
            kwargs["max_steps"] = int(p.get("max_steps") or 12)
        elif p.get("max_steps"):
            kwargs["max_steps"] = int(p["max_steps"])
    if shared_skills:
        kwargs["skills"] = shared_skills

    try:
        resp = _perplexity_client(key).responses.create(**kwargs)
        # Connector/MCP calls need the owner's decision (they ship with
        # require_approval 'always'): answer each request, then continue.
        # MAX_TOOL_TURNS bounds the decision ROUNDS, not the requests.
        rounds = 0
        while True:
            pending = [o for o in (getattr(resp, "output", None) or [])
                       if getattr(o, "type", None) == "mcp_approval_request"]
            if not pending:
                break
            if rounds >= MAX_TOOL_TURNS:
                return ("Too many approval rounds for one question - try again with "
                        "fewer shared tools.", None)
            rounds += 1
            decisions = []
            for req in pending:
                approved = False
                if confirm is not None:
                    args_txt = getattr(req, "arguments", "") or ""
                    if not isinstance(args_txt, str):
                        args_txt = _json.dumps(args_txt)
                    # The full action, never truncated - hidden arguments are
                    # worse than long ones. (Auth headers aren't part of it.)
                    approved = confirm("Perplexity tool approval",
                                        (f"Allow the Perplexity agent to run '{getattr(req, 'name', '?')}' "
                                         f"on '{getattr(req, 'server_label', '?')}'?\n\n{args_txt}"))
                d = {"type": "mcp_approval_response", "approval_request_id": getattr(req, "id", ""),
                     "approve": approved}
                if not approved:
                    d["reason"] = ("The owner didn't approve this call." if confirm is not None
                                    else "No one is available to approve this call.")
                decisions.append(d)
            # The continuation keeps the run's configuration: instructions,
            # tools and model settings are resent unchanged.
            cont = {"input": decisions, "previous_response_id": resp.id,
                    "instructions": instructions}
            if model:
                cont["model"] = model
            else:
                cont["preset"] = preset
            for k in ("tools", "max_steps", "max_output_tokens"):
                if k in kwargs:
                    cont[k] = kwargs[k]
            if shared_skills:
                cont["skills"] = shared_skills
            resp = _perplexity_client(key).responses.create(**cont)
    except _pplx.AuthenticationError:
        return ("Perplexity rejected the API key. Check PERPLEXITY_API_KEY "
                "(rotate it at console.perplexity.ai if it has been exposed).", None)
    except _pplx.RateLimitError as e:
        wait = ""
        try:
            ra = (e.response.headers.get("Retry-After") or "").strip()
            if ra.isdigit():
                wait = f" in about {int(ra)} seconds"
        except Exception:  # headers unavailable - fall back to a generic hint
            pass
        return f"Perplexity is rate-limiting requests right now - try again{wait or ' in a minute'}.", None
    except _pplx.APITimeoutError:
        return "Perplexity took too long to answer. Try a narrower question.", None
    except _pplx.APIConnectionError:
        return "Couldn't reach Perplexity - check the internet connection.", None
    except _pplx.APIStatusError as e:
        return f"Perplexity returned HTTP {e.status_code}.", None
    if getattr(resp, "status", None) == "failed":
        return "Perplexity couldn't complete that request.", None
    text = (getattr(resp, "output_text", "") or "").strip()
    if not text:
        return "No response.", None
    # A reusable conversation id only after a fully completed run.
    resp_id = getattr(resp, "id", None) if getattr(resp, "status", None) == "completed" else None
    # Sources the answer actually cites (url_citation annotations on the
    # text) - not every page the search touched.
    sources = []
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            for a in getattr(part, "annotations", None) or []:
                url = getattr(a, "url", None)
                if getattr(a, "type", None) == "url_citation" and url and url not in sources:
                    sources.append(url)
    if sources:
        text += "\n\nSources:\n" + "\n".join("- " + u for u in sources[:5])
    return text, resp_id


def perplexity_web_answer(question, cfg, confirm=None):
    """web_answer tool: one web-grounded answer for the local brain.

    Called from tools.execute_tool when the owner has enabled the tool in
    Settings (pay-per-use). The answer returns with its cited sources so
    the calling model can relay it. Shared skills/connectors from the
    perplexity config apply here too, with per-call approval."""
    instructions = ("Answer the question for a voice assistant: plain text, "
                    "concise, no markdown tables. Search the web, ground the "
                    "answer in what you find and keep the inline citations.")
    text, _ = _perplexity_call([{"type": "message", "role": "user", "content": question}],
                               cfg, instructions, confirm=confirm)
    return text


def _history_snapshot(history, cfg):
    """The raw turns the brain would replay, as comparable tuples."""
    turns = cfg.get("assistant", {}).get("history_turns", 6)
    return [(m.get("role"), m.get("content")) for m in (history or [])[-turns * 2:]]


def _run_perplexity(prompt, cfg, update_chat, history, ecosystem=None, conv_state=None):
    """Web-grounded answers through the Perplexity Agent API.

    The research loop (search, fetch, reason) runs on Perplexity's side,
    so Valerie's local function tools are NOT available through this
    brain - pick it for questions that need the live web, not for jobs
    that touch her mail, files or TV. (For local brains that still need
    the web, the web_answer tool hands individual questions over.)
    The API key is read only from the PERPLEXITY_API_KEY environment
    variable (create one at console.perplexity.ai); it is never stored
    in config.json or logged.

    conv_state is a dict the CALLER owns (the GUI keeps one per focus).
    When the same conversation continues, the prior response id is reused
    (previous_response_id) so recent turns don't need replaying; a failed
    run or a cleared history falls back to full replay automatically."""
    snap = _history_snapshot(history, cfg)
    prev_id = None
    if (conv_state is not None and conv_state.get("resp_id") and conv_state.get("tail")
            and snap == conv_state["tail"]):
        # Same conversation continuing - the server already has the context.
        prev_id = conv_state["resp_id"]
        input_items = [{"type": "message", "role": "user",
                        "content": build_user_turn(cfg, prompt, ecosystem)}]
    else:
        # Replay the recent turns as input message items, then the new
        # question (same shape the API itself documents for replay).
        input_items = [{"type": "message", "role": m["role"], "content": m["content"]}
                       for m in _history_messages(history, cfg)]
        input_items.append({"type": "message", "role": "user",
                            "content": build_user_turn(cfg, prompt, ecosystem)})
    # Request-level instructions replace only the preset's system
    # prompt; its model, tools and limits stay.
    instructions = build_system_prompt(cfg) + (
        " When a question needs current or web-wide information, "
        "search the web and ground the answer in what you find, "
        "keeping the inline source citations.")
    if update_chat:
        update_chat("Valerie", "(searching the web...)", speak=False)
    text, resp_id = _perplexity_call(input_items, cfg, instructions, confirm=CONFIRM,
                                     update_chat=update_chat, previous_response_id=prev_id)
    if conv_state is not None:
        if resp_id:
            conv_state["resp_id"] = resp_id
            conv_state["tail"] = snap + [("user", prompt), ("assistant", text)]
        else:
            conv_state.clear()   # failed run - the next turn replays in full
    return text


def _run_omniroute(prompt, cfg, update_chat, history, ecosystem=None):
    """OpenAI-compatible call through the laptop's local OmniRoute router.
    Falls back to local Ollama if OmniRoute is down or errors, so Valerie always answers."""
    import os
    o = cfg.get("omniroute", {})
    base = o.get("base_url", "http://127.0.0.1:20128/v1").rstrip("/")
    key_file = os.path.expanduser(o.get("key_file", "~/.omniroute/client.key"))
    try:
        key = open(key_file).read().strip()
    except OSError:
        return call_ollama(prompt, cfg, update_chat, history, ecosystem)
    model = o.get("model", "ollama-local/qwen2.5:3b")
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
             for t in enabled_schemas(cfg)]
    messages = [{"role": "system", "content": build_system_prompt(cfg, prompt)}]
    messages += _history_messages(history, cfg) + [{"role": "user", "content": build_user_turn(cfg, prompt, ecosystem)}]
    headers = {"Authorization": "Bearer " + key}
    try:
        for _ in range(MAX_TOOL_TURNS):
            body = {"model": model, "messages": messages}
            if tools:
                body["tools"] = tools
            r = requests.post(base + "/chat/completions", json=body, headers=headers, timeout=int(o.get("timeout", 600)))
            if r.status_code != 200:
                raise requests.RequestException(f"HTTP {r.status_code}")
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip() or "No response."
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {}) or {}
                try:
                    args = _json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if update_chat:
                    update_chat("Valerie", f"(using {fn.get('name')}...)", speak=False)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": execute_tool(fn.get("name"), args, update_chat)})
        return "Reached the tool-call limit for this turn."
    except (requests.RequestException, ValueError, KeyError, IndexError):
        if update_chat:
            update_chat("Valerie", "(OmniRoute didn't answer, using my offline brain...)", speak=False)
        return call_ollama(prompt, cfg, update_chat, history, ecosystem)


PROVIDERS = {"anthropic": _run_anthropic, "openai": _run_openai, "gemini": _run_gemini,
            "omniroute": _run_omniroute, "perplexity": _run_perplexity}


def run_agent_loop(provider, prompt, cfg=None, update_chat=None, history=None, ecosystem=None,
                  conv_state=None):
    """Entry point used by both the GUI and the headless scheduler.
    conv_state (Perplexity conversation memory) is caller-owned and only
    the perplexity provider uses it."""
    cfg = cfg or cfg_mod.load_config()
    provider = provider or cfg.get("active_provider", "ollama")
    try:
        if provider == "ollama":
            return call_ollama(prompt, cfg, update_chat, history, ecosystem)
        fn = PROVIDERS.get(provider)
        if not fn:
            return f"Unknown provider '{provider}'."
        if provider == "perplexity":
            return fn(prompt, cfg, update_chat, history, ecosystem, conv_state)
        return fn(prompt, cfg, update_chat, history, ecosystem)
    except Exception as e:  # noqa: BLE001 - never let a brain error crash the GUI
        return f"{provider} brain error: {e}"
