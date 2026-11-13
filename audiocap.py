"""Audio feeder: captures what you hear (WASAPI loopback of the default output) and/or the default
microphone, FOLLOWS default-device changes (headset plugged in, output switched, ...) and streams a
continuous 48 kHz stereo s16le signal (silence-filled) to ffmpeg over a local TCP socket.

Used by screenrec.py's worker; needs: pip install pyaudiowpatch numpy
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pyaudiowpatch as pa

RATE = 48000
CHUNK = 960  # 20 ms
PRIME = 3 * CHUNK  # jitter buffer before a source starts being consumed
MAX_BUF = RATE // 2
PA_LOCK = threading.Lock()


def _stereo(raw: bytes, ch: int, rate: int) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).astype(np.float32) / 32768.0
    if ch == 1:
        a = np.repeat(a, 2, axis=1)
    elif ch > 2:
        a = a[:, :2]
    if rate != RATE and len(a):
        n = int(len(a) * RATE / rate)
        x = np.linspace(0, len(a) - 1, n)
        a = np.stack([np.interp(x, np.arange(len(a)), a[:, i]) for i in range(2)], axis=1)
    return a.astype(np.float32)


class Source:
    """One logical source ('system' or 'mic') bound to whatever the current default device is."""

    def __init__(self, kind: str):
        self.p = self.stream = self.cur = None
        self.kind = kind
        self.name = ""
        self.lock = threading.Lock()
        self.buf = np.zeros((0, 2), dtype=np.float32)
        self.last_data = 0.0
        self.primed = False
        self.stop = False
        self.error = ""

    def _default(self, p: pa.PyAudio) -> dict:
        if self.kind == "system":
            return p.get_default_wasapi_loopback()
        host = p.get_host_api_info_by_type(pa.paWASAPI)
        return p.get_device_info_by_index(host["defaultInputDevice"])

    def _on_data(self, raw, _frames, _ti, _st, ch, rate):
        a = _stereo(raw, ch, rate)
        with self.lock:
            self.buf = np.concatenate([self.buf, a])[-MAX_BUF:]
            self.last_data = time.time()
        return (None, pa.paContinue)

    def pull(self) -> np.ndarray:
        """Next 20 ms chunk or silence."""
        with self.lock:
            if time.time() - self.last_data > 0.3:  # source idle (e.g. nothing playing)
                self.buf = self.buf[:0]
                self.primed = False
            if not self.primed and len(self.buf) >= PRIME:
                self.primed = True
            if self.primed and len(self.buf) >= CHUNK:
                out, self.buf = self.buf[:CHUNK], self.buf[CHUNK:]
                return out
            if self.primed:
                self.primed = False
        return np.zeros((CHUNK, 2), dtype=np.float32)

    def close(self) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
            if self.p is not None:
                self.p.terminate()
        except Exception:
            pass
        self.stream = self.p = self.cur = None

    def tick(self) -> None:
        try:
            probe = pa.PyAudio()
            try:
                d = self._default(probe)
            finally:
                probe.terminate()
            if self.stream is not None and (d["name"], d["index"]) != self.cur:
                self.close()
            if self.stream is None or not self.stream.is_active():
                self.close()
                p = pa.PyAudio()
                self.p = p
                d = self._default(p)
                ch = max(1, min(int(d["maxInputChannels"]), 2))
                rate = int(d["defaultSampleRate"])
                self.stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                                     input_device_index=d["index"], frames_per_buffer=rate // 100,
                                     stream_callback=lambda r, f, t, s, c=ch, rt=rate: self._on_data(r, f, t, s, c, rt))
                self.cur = (d["name"], d["index"])
                self.name = d["name"]
                self.error = ""
        except Exception as e:  # no device right now: keep trying
            self.error = str(e)
            self.name = ""
            self.close()


def manage(sources: list) -> None:
    """Single thread owning all PortAudio work (parallel PortAudio threads clash on WASAPI)."""
    while not all(s.stop for s in sources):
        for s in sources:
            s.tick()
        time.sleep(2.0)
    for s in sources:
        s.close()


class Feeder(threading.Thread):
    def __init__(self, mode: str, server: socket.socket, info_file: Path):
        super().__init__(daemon=True)
        kinds = {"system": ["system"], "mic": ["mic"]}.get(mode, ["system", "mic"])
        self.sources = [Source(k) for k in kinds]
        self.server = server
        self.info_file = info_file
        self.halt = False

    def _write_info(self) -> None:
        try:
            info = {s.kind: s.name or None for s in self.sources}
            errs = {s.kind: s.error for s in self.sources if s.error}
            if errs:
                info["errors"] = errs
            self.info_file.write_text(json.dumps(info))
        except OSError:
            pass

    def run(self) -> None:
        threading.Thread(target=manage, args=(self.sources,), daemon=True).start()
        try:
            self.server.settimeout(60)
            conn, _ = self.server.accept()
        except Exception:
            return
        nxt = time.time()
        last_info = 0.0
        while not self.halt:
            mix = sum(s.pull() for s in self.sources)
            pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            try:
                conn.sendall(pcm)
            except OSError:
                break
            nxt += CHUNK / RATE
            delay = nxt - time.time()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1.0:  # fell far behind (PC stall): resync instead of bursting
                nxt = time.time()
            if time.time() - last_info > 2:
                self._write_info()
                last_info = time.time()
        for s in self.sources:
            s.stop = True
