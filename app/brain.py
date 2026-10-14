"""
brain.py - swappable AI backend for Valerie.

Local-first: Ollama is the default and needs no account or key. Claude /
ChatGPT / Gemini remain an opt-in upgrade with your own key.

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


PROVIDERS = {"anthropic": _run_anthropic, "openai": _run_openai, "gemini": _run_gemini, "omniroute": _run_omniroute}


def run_agent_loop(provider, prompt, cfg=None, update_chat=None, history=None, ecosystem=None):
    """Entry point used by both the GUI and the headless scheduler."""
    cfg = cfg or cfg_mod.load_config()
    provider = provider or cfg.get("active_provider", "ollama")
    try:
        if provider == "ollama":
            return call_ollama(prompt, cfg, update_chat, history, ecosystem)
        fn = PROVIDERS.get(provider)
        if not fn:
            return f"Unknown provider '{provider}'."
        return fn(prompt, cfg, update_chat, history, ecosystem)
    except Exception as e:  # noqa: BLE001 - never let a brain error crash the GUI
        return f"{provider} brain error: {e}"
