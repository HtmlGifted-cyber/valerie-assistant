# Perplexity brain (web-grounded answers)

Valerie can answer through the **Perplexity Agent API** instead of a local model:
pick *Perplexity (web-grounded)* as her brain in Settings (General → AI brain).
Perplexity runs the research loop on its side — web search, fetching pages,
reasoning — and returns an answer; when the response carries citation
annotations, Valerie appends the cited sources as a list.

## When to pick it

Use it for questions that need the **live web** ("what's the latest on X?",
"compare current prices"). Two trade-offs:

- Valerie's **local tools are not available** through this brain — it can't
  read your mail, files or drive the TV. The research tools run on Perplexity's
  servers. For local jobs, use the Ollama brain.
- It's **pay-per-use** (billed per token plus a small per-search fee, cheaper
  on presets). See [Perplexity pricing](https://docs.perplexity.ai/docs/getting-started/pricing).

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

4. Settings → General → AI brain → *Perplexity (web-grounded)*.

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
  the Claude/ChatGPT/Gemini ones, registered in `PROVIDERS`.
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
