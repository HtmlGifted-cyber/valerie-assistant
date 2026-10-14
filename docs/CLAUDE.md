# Valerie and Claude

Valerie is built to run **without** any cloud AI. Claude is an optional partner in three ways.

## 1. Claude as Valerie's brain (API key)

Settings -> AI brain -> **Claude**, then paste an Anthropic API key (console.anthropic.com). Every chat turn goes to Claude with Valerie's tools, so she can still read mail, check the network and control the TV. This is billed per use on your API account.

## 2. "Ask Claude" for the hard questions (Claude Code)

If [Claude Code](https://claude.com/claude-code) is installed and signed in on the same computer, Valerie gets an **ask_claude** tool. When a question is beyond her local model ("explain this error log", "rewrite this paragraph"), she asks you first, then passes just that question to Claude Code in read-only mode and reads you the answer. It uses your own Claude plan, and Claude cannot change files from this tool.

## 3. Let Claude Code install and set up Valerie for you

Paste this into Claude Code in a terminal on the computer where Valerie should live:

```text
Install and set up Valerie Assistant from https://github.com/HtmlGifted-cyber/valerie-assistant on this
Debian/Ubuntu computer.

1. Clone the repo into ~/Projects/valerie-assistant and read README.md, docs/OMNIROUTE.md and docs/CLAUDE.md.
2. Check requirements (python3 >= 3.8, about 3 GB free disk, 6 GB RAM recommended) and tell me what's missing.
3. Build the package with packaging/build.sh. Show me the apt install command so I run the sudo step myself.
4. If Ollama isn't installed, show me the official install command and then pull qwen2.5:3b.
5. Ask me whether I want OmniRoute (free online models with offline fallback). If yes, follow docs/OMNIROUTE.md:
   install with nvm + npm under my user, set OMNIROUTE_SERVER_HOST=127.0.0.1, start it, and tell me exactly
   which dashboard steps I must click myself (login password, adding my own API keys, building the combo,
   creating the app key). Never type passwords or API keys for me and never print the key file.
6. Set "active_provider" in ~/.config/valerie-assistant/config.json to what I chose and back up the file first.
7. Test: ask Valerie one question from the command line and show me her answer.
Ask before any step that needs sudo, and keep your updates short.
```

## Using Claude Code to work *on* Valerie

The code is plain Python. Good starting points for Claude:

- `app/brain.py`: providers (`call_ollama`, `_run_omniroute`, `_run_anthropic`...) and the tool loop.
- `app/tools.py`: tool definitions. Add a tool here and in `execute_tool`.
- `app/va_network.py`: speed test and security sweep.
- `app/valerie_web.py`: the phone/browser chat.

Tip: to save Claude usage, let a cheaper or free model do the repetitive edits. With OmniRoute running, you can launch Claude Code against your own free models:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:20128 ANTHROPIC_AUTH_TOKEN="$(cat ~/.omniroute/client.key)" \
ANTHROPIC_MODEL=free-chain claude
```

Your normal `claude` command stays on your own Claude plan.

## Other assistants

The same setup works with any assistant that can run shell commands (Codex CLI, Gemini CLI, OpenCode): give it the prompt above. Valerie's only hard requirements are Python, Tk and a model endpoint.
