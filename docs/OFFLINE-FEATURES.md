# Offline features: document memory, offline library, local cameras

All three run entirely on your own computer. Nothing is sent to the cloud.

## 1. Document memory ("ask your documents")

Valerie builds a private index of the folders you choose and answers questions from your own files, telling you which file the answer came from.

**Setup (once):**
```bash
ollama pull nomic-embed-text            # 274 MB local embedding model
sudo apt install poppler-utils          # PDF text (pdftotext)
# optional, for Word/Excel/PowerPoint: uv tool install 'markitdown[all]'   (needs Python 3.10+, uv can provide it)
```
Then tell Valerie: *"add ~/Documents to my document memory"* (or edit `"docs": {"folders": [...]}` in `~/.config/valerie-assistant/config.json`).

**Use:** *"What did my notes say about cover sizes?"*, *"Find the document about the water pump."*
She re-reads only changed files: *"update my document memory"*.

Command line: `venv/bin/python app/va_docs.py index | search "question" | status`

Skipped automatically: hidden folders, `node_modules`, `venv`, `.git`, files over 25 MB, symlinks.

## 2. Offline library (Kiwix)

Offline Wikipedia, medical articles and repair guides that work with **no internet at all**, served only on `127.0.0.1:8095`.

**Setup:** the first use downloads the official `kiwix-tools` (about 21 MB, no sudo). Then download packs (you choose; they can be large):
```bash
venv/bin/python app/va_library.py packs                       # list with sizes
venv/bin/python app/va_library.py get wikipedia_en_top_mini   # ~280 MB popular articles
venv/bin/python app/va_library.py get wikipedia_en_medicine_nopic   # ~1 GB medical
venv/bin/python app/va_library.py get ifixit_en_all           # ~3.3 GB repair guides
```
Full English Wikipedia without pictures is about 49 GB. Downloads resume if interrupted and refuse to start if less than 5 GB of disk would remain.

**Use:** *"Look up how to treat a burn offline."*, *"Search the offline library for water pump."* The library server starts by itself when needed.

## 3. Local cameras and motion alerts

Use USB webcams and network cameras that offer a local stream (RTSP or a snapshot URL). Many very cheap "cloud" cameras don't: check with *find* first.

**Find cameras** (no passwords are tried): *"find cameras"* or `venv/bin/python app/va_cameras.py find`

**Add one:**
```bash
venv/bin/python app/va_cameras.py add porch "rtsp://user:pass@CAMERA-IP:554/stream1"
venv/bin/python app/va_cameras.py add desk /dev/video0
```
Passwords are masked (`***@`) in everything Valerie says or logs. Requires `ffmpeg` (`sudo apt install ffmpeg`).

**Use:** *"Take a picture from the porch camera."*, *"Any motion today?"*
**Motion watch:** `venv/bin/python app/va_cameras.py watch` checks every 20 seconds, saves pictures only when something changes, and shows a desktop alert. Tune `"motion_threshold"` (default 0.06) in config if it's too sensitive. Pictures live in `~/.local/share/valerie-assistant/cameras/` (newest 500 per camera are kept).
