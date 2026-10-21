# Perplexity brain (web-grounded answers)

Valerie can answer through the **Perplexity Agent API** instead of a local model:
pick *Perplexity (web-grounded)* as her brain in Settings (General → AI brain).
Perplexity runs the research loop on its side — web search, fetching pages,
reasoning — and returns an answer; when the response carries citation
annotations, Valerie appends the cited sources as a list.

## When to pick it

Two ways to get web-grounded answers, pick per situation:

- **The `web_answer` tool (recommended start).** Keep the local Ollama brain
  and let it hand web questions to Perplexity mid-conversation: enable
  *Web answers (Perplexity, paid API)* in Settings → Tools. Each call is a
  single researched answer with sources; Valerie's local tools keep working
  everywhere else. Off by default (pay-per-use) — flip it on once your key
  is exported.
- **The Perplexity brain.** Switch the whole brain (Settings → General → AI
  brain → *Perplexity (web-grounded)*) when you want every answer
  web-grounded. Then Valerie's local tools are NOT available - the research
  loop runs on Perplexity's side, not her mail, files or TV.

Use it for questions that need the **live web** ("what's the latest on X?",
"compare current prices"). Both paths are **pay-per-use** (billed per token
plus a small per-search fee, cheaper on presets). See
[Perplexity pricing](https://docs.perplexity.ai/docs/getting-started/pricing).

## Setup

1. Create an API key at [console.perplexity.ai](https://console.perplexity.ai).
2. Export it in the terminal that starts Valerie — the key is **only** read
   from the environment and is never stored in `config.json`, logged or
   printed:

   ```bash
   export PERPLEXITY_API_KEY="pplx-..."   # put it in ~/.bashrc to make it stick
   ```

3. Install the SDK (needs Python 3.10+; the setup wizard can do it):

   ```bash
   pip install perplexityai
   ```

4. Recommended: keep **Local (Ollama)** as the brain and enable
   **Web answers (Perplexity, paid API)** in Settings → Tools. Valerie then
   answers web questions through the `web_answer` tool while keeping all
   her local abilities.

   Alternative: switch the whole brain in Settings → General → AI brain →
   *Perplexity (web-grounded)* — every reply becomes web-grounded, but her
   local tools are not available through it.

Questions are sent to Perplexity while this is enabled, and each answer is
billed pay-per-use. When the response carries citation annotations Valerie
appends the cited sources as a list — a source list is not guaranteed with
every answer.

If the key is missing, Valerie tells you instead of calling the API. If a key
ever leaks, rotate it at [console.perplexity.ai](https://console.perplexity.ai).

## Settings

In Settings → Calendar & Cloud:

- **Preset** — `fast`, `low` (default), `medium`, `high` or `xhigh`. A preset
  bundles the model, tools and limits for you.
- **Model** (optional) — a direct model such as `openai/gpt-5.6-sol`, which
  overrides the preset and runs with the `web_search` tool. Anthropic models
  additionally require `max_output_tokens` (Valerie sends a default of 8192).
- **Share skills (names)** and **Perplexity connector IDs** — see below.

## Sharing Valerie's abilities

The three sharing lists below are **opt-in** - all off until you turn them on
(skills and connectors also have fields in Settings → Calendar & Cloud; the
rest live in `config.json` under `perplexity`). Conversation memory (further
down) is automatic for the brain.

### Skills

```json
"perplexity": {"skills": ["kdp-metadata"]}
```

Names from Valerie's skills folder
(`~/.local/share/valerie-assistant/skills/`). They're sent as Agent API
inline skills: **only the markdown instructions travel** - local scripts,
files and plugins never leave the machine. The agent sees the skill
descriptions and loads whichever the question needs (limit 16 per request;
Valerie tells you if a name doesn't exist).

### Connectors (plugins)

```json
"perplexity": {"connectors": [{"id": "connector_github", "label": "github"}]}
```

IDs from your Perplexity Project's connectors page
([console.perplexity.ai](https://console.perplexity.ai) → Project →
Connectors). The credentials live at Perplexity, never in Valerie's config.
Every connector call the agent proposes **asks first** — Valerie shows a
dialog; headless scheduled runs deny automatically. Connector calls use
Project capability plus a share of the run's steps.

### MCP servers

```json
"perplexity": {"mcp": ["server-name"]}
```

Forwards one of Valerie's remote MCP servers (from her `mcp_servers`
config) so the agent can use its tools. Public HTTPS URLs only - obvious
private/localhost targets are refused (Valerie can't verify actual cloud
reachability from the URL alone). The server's headers - including secret
files, read at call time - are sent with the request, and **every call asks
first**, exactly like connectors. A server disabled in Valerie's config
stays off for Perplexity too.

### Conversation memory

When the Perplexity brain answers a follow-up in the same conversation,
Valerie reuses the prior response id (`previous_response_id`) instead of
replaying recent turns - the server already has the context, so recent
turns aren't sent again. The state is kept per workspace focus, cleared by
`/clear`, and a failed run falls back to full replay automatically. The
`web_answer` tool stays single-shot.

`max_steps` defaults to 12 whenever skills or shared tools are in play -
enough to load a skill, call a tool and answer. Set it explicitly in
`config.json` if you want more or fewer.

## Notes for developers

- The implementation is `app/brain.py`, `_run_perplexity()` — a provider like
  the Claude/ChatGPT/Gemini ones, registered in `PROVIDERS` — plus
  `perplexity_web_answer()`, the shared core behind the `web_answer` tool
  (`app/tools.py` schema + dispatch; opt-in via `enabled_tools`).
- Sharing helpers: `_perplexity_skills()` (inline skills),
  `_perplexity_extra_tools()` (connectors + MCP forwarding, both with
  `require_approval: "always"`), and the per-call approval loop inside
  `_perplexity_call()` which answers `mcp_approval_request` items through the
  GUI's confirm callback (denied when headless). Conversation memory:
  `run_agent_loop(..., conv_state=<caller-owned dict>)` - the GUI keeps one
  per workspace focus.
- Multi-turn works by replaying the recent conversation as input message items
  (the API's documented replay shape); the answer is read from `output_text`,
  and cited URLs come from `url_citation` annotations on the text (deduplicated,
  capped at five - no annotations means no source list).
- Errors are sanitized into friendly strings. The SDK's automatic retries are
  disabled for this brain; on a 429 Valerie reads the server's `Retry-After`
  interval and tells you when to try again instead of retrying prematurely.
- `tests/test_perplexity.py` runs the whole brain against a local fake API
  server — no network, no real key:

  ```bash
  python3 tests/test_perplexity.py
  ```
