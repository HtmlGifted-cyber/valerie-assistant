# Valerie Assistant

A personal AI assistant for Linux that runs on your own computer. She chats through a local AI model, keeps your work areas separate, handles email, runs daily check-ins, talks and listens hands-free, and can even work your TV.

Built and maintained by Jeremy A. Henry ([@HtmlGifted-cyber](https://github.com/HtmlGifted-cyber)).

## What she does

- **Local AI chat.** Uses [Ollama](https://ollama.com) (default model `qwen2.5:3b`) on your own machine. Claude, ChatGPT or Gemini are optional with your own key.
- **Workspaces kept separate.** Each one has its own mailbox, task board, memory, knowledge and time zone, and they never mix.
- **Email.** Works through IMAP/SMTP or Thunderbird. She reads your mail and drafts replies; nothing is sent without your approval.
- **Voice.** Read aloud, dictation, and a hands-free Talk mode with a wake word. Spoken briefings are natural sentences, not data read back.
- **Check-ins.** Morning, midday and evening rounds of inboxes, boards, a daily report file, backups and system health.
- **Daily report.** Point her at a folder that receives a dated `Ecosystem-Report_YYYY-MM-DD.txt`. She reads it when you say "good morning" and won't treat an old one as today's news.
- **TV control.** Roku TVs on your network: "mute the TV", "put on YouTube on the TV", "TV volume up 3".
- **Skills, knowledge and connectors.** Reusable prompt skills, searchable knowledge folders, plug-in connectors for task boards and web APIs, web search and a file finder.
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

## Project layout

| Path | What's there |
|---|---|
| `app/` | The assistant (Tk GUI, AI brain, tools, mail, voice, scheduler, connectors) |
| `defaults/` | Starter skills, knowledge (getting started and the VA playbook) and example connectors |
| `packaging/` | `build.sh` (makes the .deb), setup wizard, launcher, desktop entry, icon |

## Your data stays yours

- Settings: `~/.config/valerie-assistant/config.json` (readable by your account only)
- Skills, notes, schedule and mail archive: `~/.local/share/valerie-assistant/`
- Uninstalling leaves your data in place; delete those folders to remove it.

**Privacy gate.** `build.sh` refuses to build if the package contains personal data. Put your own names, emails and IDs (one regex per line) in `~/.config/valerie-assistant/privacy-patterns.txt`. That file is never part of the repo.

## Credits

Voice by [Piper](https://github.com/rhasspy/piper) (MIT) with the `en_US-amy-medium` voice. Local AI by Ollama (MIT) and Qwen2.5 (Apache 2.0). All are downloaded at setup, not bundled.
