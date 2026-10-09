"""
voices.py - Piper voice catalog + on-demand downloader.

Valerie ships with one voice already fetched at install time (amy-medium,
same voice 1.0.0 used - it's serviceable but, fairly, a bit flat/robotic).
Everything else here is fetched only if you pick it in Settings, and it's
a one-time, local file copy from Piper's public voice release - not a
cloud TTS call: once a voice's .onnx file is on disk, speaking never
touches the network again, with or without picking a "better" voice.

Piper is a small neural vocoder, not a giant cloud model, so none of
these will sound quite like a cloud voice assistant - but "high" quality
variants and some prosody tuning (see config.py's voice.noise_scale/
noise_w) go a meaningful way toward sounding less like a GPS unit.
"""

import os

import requests

# Downloads go to the user's own folder (the /usr/share one is root-owned,
# which is why "Download voice" used to fail); the bundled voice in the
# system folder is still found.
VOICES_DIR = os.path.expanduser("~/.local/share/valerie-assistant/voices")
SYSTEM_VOICES_DIR = "/usr/share/valerie-assistant/voices"
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en"

# id -> (human label, path fragment under HF_BASE)
CATALOG = {
    "en_US-amy-medium": (
        "Amy (US female, medium) - the original default",
        "en_US/amy/medium",
    ),
    "en_US-lessac-medium": (
        "Lessac (US male, medium) - warmer, less clipped than Amy",
        "en_US/lessac/medium",
    ),
    "en_US-ljspeech-high": (
        "LJSpeech (US female, HIGH quality) - most natural of this set, bigger + slower to synthesize",
        "en_US/ljspeech/high",
    ),
    "en_US-hfc_female-medium": (
        "HFC Female (US, medium) - softer tone",
        "en_US/hfc_female/medium",
    ),
    "en_GB-alba-medium": (
        "Alba (British female, medium)",
        "en_GB/alba/medium",
    ),
}


def labels():
    """id -> display label, for populating a Settings dropdown."""
    return {vid: label for vid, (label, _path) in CATALOG.items()}


def voice_files(voice_id):
    sys_onnx = os.path.join(SYSTEM_VOICES_DIR, f"{voice_id}.onnx")
    if os.path.exists(sys_onnx) and os.path.exists(sys_onnx + ".json"):
        return sys_onnx, sys_onnx + ".json"
    return (
        os.path.join(VOICES_DIR, f"{voice_id}.onnx"),
        os.path.join(VOICES_DIR, f"{voice_id}.onnx.json"),
    )


def is_downloaded(voice_id):
    onnx, cfg_json = voice_files(voice_id)
    return os.path.exists(onnx) and os.path.exists(cfg_json)


def download(voice_id, progress_cb=None):
    """Fetch a voice's .onnx + .onnx.json into VOICES_DIR if not already
    present. progress_cb(str), if given, is called with short status
    text suitable for showing in the GUI."""
    if voice_id not in CATALOG:
        raise ValueError(f"Unknown voice '{voice_id}'")
    os.makedirs(VOICES_DIR, exist_ok=True)
    _label, path_fragment = CATALOG[voice_id]
    base_url = f"{HF_BASE}/{path_fragment}/{voice_id}"
    onnx_path, json_path = voice_files(voice_id)

    for url, dest in [(f"{base_url}.onnx", onnx_path), (f"{base_url}.onnx.json", json_path)]:
        if os.path.exists(dest):
            continue
        if progress_cb:
            progress_cb(f"Downloading {os.path.basename(dest)}...")
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        tmp_dest = dest + ".part"
        with open(tmp_dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        os.replace(tmp_dest, dest)

    return onnx_path
