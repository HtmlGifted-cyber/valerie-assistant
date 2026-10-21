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

## Notes for developers

- The implementation is `app/brain.py`, `_run_perplexity()` — a provider like
  the Claude/ChatGPT/Gemini ones, registered in `PROVIDERS` — plus
  `perplexity_web_answer()`, the shared core behind the `web_answer` tool
  (`app/tools.py` schema + dispatch; opt-in via `enabled_tools`).
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
