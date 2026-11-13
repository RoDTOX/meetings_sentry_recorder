"""Voice watcher ("call/meeting auto-record"): listens to the system output (other people) and the
microphone (you) with a speech detector (WebRTC VAD, not plain loudness) and starts a light recording
(audio + 1-2 fps screenshots) when a real conversation is detected; stops after a stretch of silence.

Used by screenrec.py (voice-start); needs: pip install webrtcvad-wheels pyaudiowpatch numpy
Trigger modes:
  conversation  speech on BOTH streams within the window (you + someone else) -> ignores videos/music
                playing alone and you talking to yourself. Default.
  any           speech on either stream (also catches a meeting where you stay muted).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import webrtcvad

import audiocap

FRAME_S = 0.02
WINDOW_S = 20  # sliding window used for the trigger
PREROLL_S = 20
RMS_GATE = 0.008  # ~ -42 dBFS: below this a frame never counts as speech
TMP = Path(tempfile.gettempdir())
STATUS = TMP / "screenrec_voice.status.json"


class Stream:
    def __init__(self, kind: str):
        self.src = audiocap.Source(kind)
        self.vad = webrtcvad.Vad(3)
        self.hist: deque[bool] = deque(maxlen=int(WINDOW_S / FRAME_S))
        self.level = 0.0

    def feed(self) -> None:
        a = self.src.pull()  # 20 ms, 48 kHz stereo float
        mono = a.mean(axis=1)
        self.level = float(np.sqrt(np.mean(mono ** 2)))
        m16 = mono.reshape(-1, 3).mean(axis=1)  # 48k -> 16k
        self.pcm = (np.clip(m16, -1, 1) * 32767).astype(np.int16)
        speech = self.level > RMS_GATE and self.vad.is_speech(self.pcm.tobytes(), 16000)
        self.hist.append(speech)

    def speech_s(self, last_s: float = WINDOW_S) -> float:
        n = int(last_s / FRAME_S)
        return sum(list(self.hist)[-n:]) * FRAME_S


def _open_audio_writer(out: str):
    """ffmpeg reading 16 kHz stereo s16le (L = you, R = others) from stdin -> m4a for transcription."""
    from screenrec import ffmpeg_path, NOWIN
    return subprocess.Popen([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", "16000",
                             "-ac", "2", "-i", "-", "-c:a", "aac", "-b:a", "64k", out],
                            stdin=subprocess.PIPE, creationflags=NOWIN)


def _close_audio_writer(w) -> None:
    try:
        w.stdin.close()
        w.wait(timeout=30)
    except Exception:
        w.kill()


def run(cfg: dict, start_rec, stop_rec, rec_running, flag: Path) -> None:
    """cfg: mode, hangover_s, min_speech_s, fps, max_height, target, out_dir, max_minutes.
    start_rec(out) / stop_rec() / rec_running() are provided by screenrec.py."""
    streams = {"mic": Stream("mic"), "system": Stream("system")}
    srcs = [s.src for s in streams.values()]
    threading.Thread(target=audiocap.manage, args=(srcs,), daemon=True).start()

    preroll: deque[bytes] = deque(maxlen=int(PREROLL_S / FRAME_S))  # speech before the trigger is kept
    writer = None
    m4a = None
    recording = False
    own_out = None
    rec_started = 0.0
    last_voice = 0.0
    meetings: list[dict] = []
    nxt = time.time()
    last_eval = 0.0

    def finish() -> None:
        nonlocal writer, recording
        r = stop_rec() if own_out else {}
        if writer:
            _close_audio_writer(writer)
        writer = None
        meetings.append({"video": own_out, "audio": m4a, "duration_s": r.get("duration_s") or round(time.time() - rec_started)})
        recording = False
        for s in streams.values():  # old speech must not re-trigger a new recording
            s.hist.clear()
        preroll.clear()

    while not flag.exists():
        for s in streams.values():
            s.feed()
        frame = np.stack([streams["mic"].pcm, streams["system"].pcm], axis=1).tobytes()
        if writer:
            try:
                writer.stdin.write(frame)
            except OSError:
                writer = None
        else:
            preroll.append(frame)
        nxt += FRAME_S
        d = nxt - time.time()
        if d > 0:
            time.sleep(d)
        elif d < -1:
            nxt = time.time()
        now = time.time()
        if now - last_eval < 1.0:
            continue
        last_eval = now
        mic_s, sys_s = streams["mic"].speech_s(), streams["system"].speech_s()
        need = cfg["min_speech_s"]
        if cfg["mode"] == "any":
            triggered = max(mic_s, sys_s) >= need * 2
        else:
            triggered = mic_s >= need and sys_s >= need
        talking = max(streams["mic"].speech_s(2), streams["system"].speech_s(2)) >= 0.4
        if talking:
            last_voice = now

        if not recording and triggered and not rec_running():
            stem = Path(cfg["out_dir"]) / f"call_{time.strftime('%Y%m%d_%H%M%S')}"
            m4a = str(stem.with_suffix(".m4a"))
            writer = _open_audio_writer(m4a)
            writer.stdin.write(b"".join(preroll))
            preroll.clear()
            recording, rec_started, last_voice = True, now, now
            own_out = None
            if cfg.get("content", "audio") == "audio+screen":
                own_out = start_rec(str(stem.with_suffix(".mp4"))).get("out")
        elif recording:
            if now - last_voice > cfg["hangover_s"] or now - rec_started > cfg["max_minutes"] * 60:
                finish()

        try:
            STATUS.write_text(json.dumps({
                "watching": True, "mode": cfg["mode"], "recording": recording, "current": m4a if recording else None,
                "recording_s": round(now - rec_started) if recording else 0,
                "speech_s_20s": {"mic": round(mic_s, 1), "system": round(sys_s, 1)},
                "last_voice_ago_s": round(now - last_voice) if last_voice else None,
                "devices": {k: s.src.name or None for k, s in streams.items()},
                "meetings": meetings[-10:]}))
        except OSError:
            pass
    if recording:
        finish()
    for s in srcs:
        s.stop = True
    try:
        STATUS.write_text(json.dumps({"watching": False, "meetings": meetings[-10:]}))
    except OSError:
        pass
