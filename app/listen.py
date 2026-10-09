"""
listen.py - local speech-to-text, so Valerie can hear you.

Fully offline inference via faster-whisper (CTranslate2), running on
this machine's CPU - no account, no API key, no per-use network call.
The model WEIGHTS are fetched once from Hugging Face the first time you
use a given size (tiny/base/small) and cached under ~/.cache/huggingface;
every transcription after that first download runs with zero network
activity. That one-time fetch is a local file download, the same kind
of thing installing a dictionary or a spellchecker needs - it is not
Valerie "calling a cloud AI platform" the way the optional Claude/
ChatGPT/Gemini providers in brain.py do.

Usage (see gui_bot.py's mic button):
    rec = Recorder()
    rec.start()      # begin capturing from the default microphone
    ...               # user is talking
    audio = rec.stop()           # numpy float32 array @ 16kHz mono
    text = transcribe(audio)     # -> recognized text
"""

try:
    import numpy as np
except ImportError:
    np = None  # degrades to "mic unavailable" instead of crashing the whole GUI at import time

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

SAMPLE_RATE = 16000
_MODEL_CACHE = {}


def available():
    return np is not None and sd is not None and WhisperModel is not None


def missing_pieces():
    missing = []
    if np is None:
        missing.append("numpy")
    if sd is None:
        missing.append("sounddevice")
    if WhisperModel is None:
        missing.append("faster-whisper")
    return missing


def _get_model(size):
    if WhisperModel is None:
        return None
    if size not in _MODEL_CACHE:
        kw = dict(device="cpu", compute_type="int8", cpu_threads=4)
        try:
            # Cached model: load straight from disk, no Hugging Face network check
            # (that check could stall for ages on slow campground Wi-Fi).
            _MODEL_CACHE[size] = WhisperModel(size, local_files_only=True, **kw)
        except Exception:
            _MODEL_CACHE[size] = WhisperModel(size, **kw)  # first use: download once
    return _MODEL_CACHE[size]


def preload(size):
    """Load the model in the background at startup so the first
    'Hold to Talk' doesn't pay the load time."""
    try:
        _get_model(size)
    except Exception:
        pass


class Recorder:
    """Push-to-talk style microphone capture: start() while the user
    holds the mic button, stop() on release to get the recorded audio."""

    def __init__(self):
        self._frames = []
        self._stream = None

    def start(self):
        if np is None or sd is None:
            missing = missing_pieces()
            raise RuntimeError(
                f"Microphone input needs: {', '.join(missing)}. Run: "
                f"/usr/share/valerie-assistant/venv/bin/pip install {' '.join(missing)}"
            )
        self._frames = []

        def _callback(indata, _frames, _time_info, _status):
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=_callback
        )
        self._stream.start()

    def stop(self):
        if np is None:
            return None
        if self._stream is None:
            return np.zeros(0, dtype="float32")
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        audio = np.concatenate(self._frames, axis=0).flatten()
        self._frames = []
        return audio


def transcribe(audio, model_size="base", strict=False):
    """audio: numpy float32 array @ 16kHz mono, as returned by Recorder.stop()."""
    if WhisperModel is None:
        return "[faster-whisper isn't installed - run: venv/bin/pip install faster-whisper]"
    if np is None:
        return "[numpy isn't installed - run: venv/bin/pip install numpy]"
    if audio is None or len(audio) < SAMPLE_RATE * 0.3:
        return ""  # too short to be real speech

    try:
        model = _get_model(model_size)
        if model is None:
            return ""
        opts = dict(language="en", beam_size=1, without_timestamps=True, condition_on_previous_text=False)
        try:
            segments, _info = model.transcribe(audio, vad_filter=True, **opts)
            segments = list(segments)
        except Exception:
            # VAD needs onnxruntime; if that ever breaks, transcribe without it
            # rather than failing (this is what used to hang the mic button).
            segments, _info = model.transcribe(audio, vad_filter=False, **opts)
            segments = list(segments)
        if strict:
            # Talk mode: drop fragments Whisper itself thinks aren't speech or
            # isn't confident about (room noise often comes out as "you",
            # "together.", "Thank you." etc.).
            segments = [seg for seg in segments
                        if getattr(seg, "no_speech_prob", 0) < 0.6 and getattr(seg, "avg_logprob", 0) > -1.0]
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:  # noqa: BLE001 - never leave the GUI stuck on "Transcribing..."
        return f"[speech recognition error: {e}]"


def _has_speech(audio):
    """Silero speech detector (bundled with faster-whisper): tells real
    speech apart from fans, TV and room noise. ~40 ms per call."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    segs = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=250, speech_pad_ms=0,
                                                   min_speech_duration_ms=200))
    return bool(segs)


def listen_utterance(stop_event=None, max_seconds=30, silence_seconds=1.2, on_state=None, keep_on_stop=False,
                     abort_if=None):
    """Talk mode: wait until the owner starts speaking, record until ~1.2 s with
    no speech, return the audio (None if stopped). Uses the Silero speech
    detector so background noise doesn't keep the recording open; falls back
    to a loudness check if the detector can't load."""
    if np is None or sd is None:
        return None
    import queue
    import time
    q = queue.Queue()
    block = int(SAMPLE_RATE * 0.1)  # 100 ms blocks

    def cb(indata, _frames, _t, _status):
        q.put(indata.copy().flatten())

    try:
        _has_speech(np.zeros(SAMPLE_RATE, dtype="float32"))
        detector = _has_speech
    except Exception:
        detector = None

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block, callback=cb)
    stream.start()
    try:
        buf, speaking, start_idx, started = [], False, 0, time.time()
        noise = None
        last_check = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                if keep_on_stop and speaking:
                    break  # manual stop while talking: keep what was said
                return None
            if abort_if is not None and abort_if():
                return None   # Valerie started talking - don't record her own voice
            try:
                buf.append(q.get(timeout=0.5))
            except queue.Empty:
                continue
            n = len(buf)
            if not speaking and n > 300:          # keep only the last 30 s while waiting
                buf, n = buf[-20:], 20
            if n - last_check < 3:                # check every 0.3 s
                continue
            last_check = n
            if detector is not None:
                window = np.concatenate(buf[-12:])  # last 1.2 s
                heard = detector(window)
            else:
                level = float(np.sqrt(np.mean(buf[-1] ** 2)))
                noise = level if noise is None else 0.95 * noise + 0.05 * level
                heard = level > max(noise * 2.5, 0.02)
            if not speaking:
                if heard:
                    speaking, start_idx, started = True, max(0, n - 12), time.time()
                    if on_state:
                        on_state("Hearing you...")
                continue
            if not heard and (time.time() - started) > silence_seconds:
                break
            if time.time() - started > max_seconds:
                break
        audio = np.concatenate(buf[start_idx:])
        return audio if len(audio) > SAMPLE_RATE * 0.5 else None
    finally:
        stream.stop()
        stream.close()
