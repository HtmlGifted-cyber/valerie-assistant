# Using Valerie with OmniRoute (free AI models, automatic fallback)

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) is a small router that runs on your own computer. You connect AI providers to it once (Google Gemini's free tier, Groq, OpenRouter, your local Ollama, ...) and every app talks to one address. When a provider hits its free limit or goes down, OmniRoute moves on to the next one.

For Valerie this means: **stronger answers than a small local model when you're online, at no cost, and she still works offline.**

```
Valerie ──> OmniRoute (localhost:20128) ──> Gemini (free) ──> Gemini Flash-Lite ──> Ollama on your computer
                    if OmniRoute itself is down ─────────────────────────────────────> Ollama directly
```

## 1. Install OmniRoute (needs Node.js 20+)

Ubuntu 20.04 ships an old Node. Install a current one just for your user with [nvm](https://github.com/nvm-sh/nvm), so nothing system-wide changes:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash   # then open a new terminal
nvm install 22
npm install -g omniroute
```

**Keep it private to your computer.** By default OmniRoute listens on your whole home network. Lock it to this machine:

```bash
mkdir -p ~/.omniroute && echo 'OMNIROUTE_SERVER_HOST=127.0.0.1' >> ~/.omniroute/.env
omniroute serve --daemon
omniroute autostart enable     # optional: start with your computer
```

## 2. One-time setup in the OmniRoute dashboard (manual steps)

Open **http://localhost:20128** in your browser.

1. **Log in.** The first-login password was generated at install time:
   `grep INITIAL_PASSWORD "$(npm root -g)/omniroute/.env"`. Change it right away.
2. **Add your providers** (Providers page, use the search box):
   - **Ollama**: base URL `http://localhost:11434/v1`, no key. Click *Import from models*.
   - **Gemini (Google AI Studio)**: get a free key at <https://aistudio.google.com> -> *Get API key*. Turn on **Import only free models**, paste the key, *Check*, *Save*, and make sure the connection switch is **on**.
   - Optional free extras: **Groq** (console.groq.com), **OpenRouter** (`:free` models).
3. **Build a fallback combo** (Combos -> Create Combo, strategy *Priority*). Example named `free-chain`:
   Gemini 3.8 Flash -> Gemini 3.7 Flash -> Gemini 3.1 Flash-Lite -> Ollama `qwen2.5:3b`.
   Free tiers allow only a few requests per minute; the chain keeps answering when one is "cooling down".
4. **Create an API key for your apps** (API Keys -> Create API Key, e.g. `laptop-apps`), click *Copy*, and save it where only you can read it:

```bash
umask 077; nano ~/.omniroute/client.key     # paste the key, save
```

> **Skip the built-in "no-auth" / web-relay providers** for anything private. Many are unofficial relays run by strangers; whatever you send passes through their servers. Also avoid connecting a paid *subscription login* (Claude Pro, ChatGPT Plus) to a third-party router: that is usually against the provider's terms.

## 3. Point Valerie at it

Valerie -> **Settings -> AI brain -> OmniRoute (free models)**, or edit `~/.config/valerie-assistant/config.json`:

```json
"active_provider": "omniroute",
"omniroute": {
  "base_url": "http://127.0.0.1:20128/v1",
  "key_file": "~/.omniroute/client.key",
  "model": "free-chain",
  "timeout": 240
}
```

`model` is a combo name or a single model id such as `gemini/gemini-3.8-flash`. Restart Valerie.

## 4. Check it works

```bash
curl -s http://127.0.0.1:20128/v1/chat/completions \
  -H "Authorization: Bearer $(cat ~/.omniroute/client.key)" -H "Content-Type: application/json" \
  -d '{"model":"free-chain","messages":[{"role":"user","content":"Say: ok"}]}'
```

Then ask Valerie anything. If OmniRoute is stopped she says *"OmniRoute didn't answer, using my offline brain"* and answers with Ollama.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `All credentials ... are cooling down` | Free per-minute limit hit. Wait a minute, or add more providers / a longer combo. |
| `Authentication required` | The key in `~/.omniroute/client.key` is missing or wrong. Create a new one in *API Keys*. |
| Gemini model `404 ... no longer available` | Google retires model names. Pick a current one in the provider's model list. |
| Very slow answers | The chain fell back to your local model. That's normal while online providers cool down. |
| Reachable from other devices | `OMNIROUTE_SERVER_HOST=127.0.0.1` isn't set; see step 1. |
