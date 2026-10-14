# Valerie Assistant

A personal AI assistant for Linux that runs on your own computer. She chats through a local AI model, keeps your work areas separate, handles email, runs daily check-ins, talks and listens hands-free, and can even work your TV.

Built and maintained by Jeremy A. Henry ([@HtmlGifted-cyber](https://github.com/HtmlGifted-cyber)).

## What she does

- **Local AI chat.** Uses [Ollama](https://ollama.com) (default model `qwen2.5:3b`) on your own machine. Claude, ChatGPT or Gemini are optional with your own key.
- **Free online brain with offline fallback (new).** Point her at a local [OmniRoute](https://github.com/diegosouzapw/OmniRoute) router and she uses free models such as Google Gemini, chaining to the next provider when one hits its limit, and back to Ollama when you're offline. See [docs/OMNIROUTE.md](docs/OMNIROUTE.md).
- **Ask your documents (new, offline).** She indexes the folders you choose (PDF, Word, Excel, notes) with a local embedding model and answers from your own files, citing the file. See [docs/OFFLINE-FEATURES.md](docs/OFFLINE-FEATURES.md).
- **Offline library (new).** Offline Wikipedia, medical and repair guides via Kiwix, so she can still look things up with no internet at all.
- **Local cameras (new).** Finds USB and network cameras, takes snapshots and watches for motion with desktop alerts. No cloud, pictures stay on your computer.
- **Works with Claude.** Use Claude as her brain, let her "ask Claude" for hard questions through Claude Code, or have Claude Code install her for you with one prompt. See [docs/CLAUDE.md](docs/CLAUDE.md).
- **Workspaces kept separate.** Each one has its own mailbox, task board, memory, knowledge and time zone, and they never mix.
- **Email.** Works through IMAP/SMTP or Thunderbird. She reads your mail and drafts replies; nothing is sent without your approval.
- **Voice.** Read aloud, dictation, and a hands-free Talk mode with a wake word. Spoken briefings are natural sentences, not data read back.
- **Check-ins.** Morning, midday and evening rounds of inboxes, boards, a daily report file, backups and system health.
- **Daily report.** Point her at a folder that receives a dated `Ecosystem-Report_YYYY-MM-DD.txt`. She reads it when you say "good morning" and won't treat an old one as today's news.
- **TV control.** Roku TVs on your network: "mute the TV", "put on YouTube on the TV", "TV volume up 3".
- **Skills, knowledge and connectors.** Reusable prompt skills, searchable knowledge folders, plug-in connectors for task boards and web APIs, web search and a file finder.
- **Phone and browser chat.** `valerie-web` serves a PIN-protected chat page on your home network (works without the desktop window). You see what she's doing while she thinks, and which tools she used.
- **Home network guardian.** "Why is my internet slow?" runs a real speed test and shows what's using the connection. "Am I hacked?" checks new listening ports, remote logins, failed logins, new startup programs and unknown devices against your saved normal baseline.
- **VA playbook.** A built-in guide to tone, pushback and reply style, so she acts like a good assistant rather than a chatbot.

## Install (Debian / Ubuntu)

Build the package:

```bash
git clone https://github.com/HtmlGifted-cyber/valerie-assistant.git
cd valerie-assistant/packaging && ./build.sh
sudo apt install ./valerie-assistant_*_all.deb
```

Then open **Valerie Assistant** from your menu. The first launch runs a setup wizard that downloads her voice and components into your home folder, helps install Ollama if needed, and asks your name and workspaces. Nothing runs as root.

**Requirements:** Ubuntu 20.04 or newer (or another Debian-based system), Python 3.8+, x86_64 or ARM64, 6 GB RAM recommended, about 3 GB free disk.

## Quick setup with an AI coding assistant

Using Claude Code, Codex or a similar assistant? Paste the setup prompt from [docs/CLAUDE.md](docs/CLAUDE.md#3-let-claude-code-install-and-set-up-valerie-for-you). It walks through install, Ollama, optional OmniRoute and a test, and stops for you at every sudo step and every password or key.

## Help make her better

Valerie is young and grows from real use. If something breaks, confuses you, or you wish she could do something, please [open an issue](https://github.com/HtmlGifted-cyber/valerie-assistant/issues) with:
- what you asked or clicked, and what happened,
- your OS version and which AI brain you use (Ollama / OmniRoute / Claude / ...),
- the last lines of `~/.local/share/valerie-assistant/gui.log` (remove anything private).

Feature ideas and pull requests are welcome.

## Project layout

| Path | What's there |
|---|---|
| `app/` | The assistant (Tk GUI, AI brain, tools, mail, voice, scheduler, connectors) |
| `defaults/` | Starter skills, knowledge (getting started and the VA playbook) and example connectors |
| `docs/` | Setup guides: [OmniRoute](docs/OMNIROUTE.md), [Claude](docs/CLAUDE.md), [offline features](docs/OFFLINE-FEATURES.md) |
| `packaging/` | `build.sh` (makes the .deb), setup wizard, launcher, desktop entry, icon |

## Your data stays yours

- Settings: `~/.config/valerie-assistant/config.json` (readable by your account only)
- Skills, notes, schedule and mail archive: `~/.local/share/valerie-assistant/`
- Uninstalling leaves your data in place; delete those folders to remove it.

**Privacy gate.** `build.sh` refuses to build if the package contains personal data. Put your own names, emails and IDs (one regex per line) in `~/.config/valerie-assistant/privacy-patterns.txt`. That file is never part of the repo.

## Credits

Voice by [Piper](https://github.com/rhasspy/piper) (MIT) with the `en_US-amy-medium` voice. Local AI by Ollama (MIT) and Qwen2.5 (Apache 2.0). All are downloaded at setup, not bundled.
