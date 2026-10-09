# Valerie Assistant

A personal AI assistant for Linux that runs on your own computer.

## Install
    sudo apt install ./valerie-assistant_*_all.deb

Then open **Valerie Assistant** from your applications menu. The first launch runs a short
setup that downloads Valerie's components into your home folder, helps you install the
local AI (Ollama) if needed, and asks your name and workspaces.

## What it does
- Chat with a local AI (Ollama, default model qwen2.5:3b) - optional Claude / ChatGPT / Gemini with your own key
- Workspaces kept strictly separate: own mailbox, task board, memory, knowledge and time zone
- Email via your mail server (IMAP/SMTP) or Thunderbird; nothing is sent without your approval
- Voice: read aloud, dictation, hands-free Talk mode with wake word; STOP / Pause anytime
- Reminders and scheduled check-ins per time zone; calendar (private iCal link)
- Skills (reusable prompts), knowledge folders, connectors/plug-ins, web search, file finder

## Requirements
Ubuntu 20.04 or newer (or Debian-based equivalent), x86_64 or ARM64, 6 GB RAM recommended,
about 3 GB free disk space, internet for setup.

## Your data
Settings: ~/.config/valerie-assistant/config.json (private to your account).
Skills, notes, schedule, mail archive: ~/.local/share/valerie-assistant/.
Uninstalling the package leaves your data; delete those folders to remove it.
