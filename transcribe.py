"""Offline transcription of call recordings (faster-whisper, CPU, Romanian + English).

Input: call_*.m4a made by voicewatch (stereo, left = you, right = others). Each channel is transcribed
separately, so speakers are labelled without diarization, then merged by time.
Output next to the audio:
  call_*.txt   readable:  [00:01:23] You: ...  /  [00:01:30] Others: ...
  call_*.json  same segments + metadata, for parsing with another AI

Needs: pip install faster-whisper numpy. The model downloads on first use (turbo ~1.6 GB).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

SR = 16000
DEFAULT_MODEL = os.environ.get("SCREENREC_WHISPER_MODEL", "large-v3-turbo")
LOCK = Path(tempfile.gettempdir()) / "screenrec_transcribe.lock"
LABELS = ("You", "Others")


def _decode(path: str) -> np.ndarray:
    """-> float32 array (n, channels) at 16 kHz; mono files come back as (n, 1)."""
    from screenrec import ffmpeg_path, NOWIN
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path], capture_output=True, text=True,
                           creationflags=NOWIN).stderr
    ch = 1 if "mono" in probe else 2
    raw = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", path, "-f", "f32le",
                          "-ac", str(ch), "-ar", str(SR), "-"], capture_output=True, creationflags=NOWIN).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, ch)


def _ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _wait_turn() -> None:
    """One transcription at a time: they are CPU heavy."""
    from screenrec import alive
    while LOCK.exists():
        try:
            if not alive(int(LOCK.read_text())):
                break
        except (ValueError, OSError):
            break
        time.sleep(5)
    LOCK.write_text(str(os.getpid()))


def _drop_mic_bleed(segs: list[dict]) -> list[dict]:
    """Without a headset the mic re-records the speakers: drop a 'You' segment that repeats an overlapping 'Others' one."""
    from difflib import SequenceMatcher
    others = [s for s in segs if s["speaker"] == "Others"]
    out = []
    for s in segs:
        if s["speaker"] == "You" and any(
                o["start"] < s["end"] + 1.5 and s["start"] < o["end"] + 1.5 and
                SequenceMatcher(None, s["text"].lower(), o["text"].lower()).ratio() > 0.6 for o in others):
            continue
        out.append(s)
    return out


def transcribe_file(path: str, model: str = DEFAULT_MODEL, language: str | None = None) -> dict:
    """language: 'ro' | 'en' | None (auto-detect per channel)."""
    src = Path(path)
    if not src.exists():
        return {"ok": False, "error": f"not found: {path}"}
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        return {"ok": False, "error": f"needs: pip install faster-whisper ({e})"}
    _wait_turn()
    try:
        t0 = time.time()
        audio = _decode(str(src))
        if audio.size == 0:
            return {"ok": False, "error": "no audio decoded"}
        wm = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=max(4, (os.cpu_count() or 8) // 2))
        segs, langs = [], {}
        for i in range(audio.shape[1]):
            who = LABELS[i] if audio.shape[1] == 2 else "Speaker"
            x = np.ascontiguousarray(audio[:, i])
            if float(np.sqrt(np.mean(x ** 2))) < 1e-4:
                continue  # silent channel
            it, info = wm.transcribe(x, language=language, vad_filter=True, beam_size=5,
                                     condition_on_previous_text=False)
            langs[who] = info.language
            for s in it:
                text = s.text.strip()
                if text:
                    segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "speaker": who, "text": text})
        segs.sort(key=lambda s: s["start"])
        segs = _drop_mic_bleed(segs)
        txt = src.with_suffix(".txt")
        txt.write_text("\n".join(f"[{_ts(s['start'])}] {s['speaker']}: {s['text']}" for s in segs) + "\n",
                       encoding="utf-8")
        meta = {"audio": str(src), "model": model, "languages": langs, "duration_s": round(len(audio) / SR, 1),
                "transcribed_in_s": round(time.time() - t0, 1), "channels": "left=You, right=Others",
                "segments": segs}
        src.with_suffix(".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "transcript": str(txt), "json": str(src.with_suffix(".json")), "segments": len(segs),
                "languages": langs, "duration_s": meta["duration_s"], "took_s": meta["transcribed_in_s"]}
    finally:
        LOCK.unlink(missing_ok=True)
