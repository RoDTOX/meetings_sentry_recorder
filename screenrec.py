#!/usr/bin/env python3
"""Headless screen recorder for AI agents / scripts (Windows, ffmpeg gdigrab).

Commands (all print JSON on stdout):
  devices                         monitors + audio devices + available encoders
  start [--target all|monitor:N|region:X,Y,W,H] [--audio auto|system|mic|none|<dshow name>]
        [--fps 30] [--max-height 1080|1440|0] [--encoder auto|qsv|nvenc|amf|x265|x264]
        [--format mp4|mkv] [--out PATH] [--max-seconds N]     start in background
  stop                            graceful stop (finalizes the file), returns path/size
  status                          is a recording running, elapsed, file size
  screenshot [--target ...] [--out PATH.png]
  list [--limit 10]               recent recordings in the output folder
  sentry-start [--buffer-minutes 30] [--fps 5] [--target ..] [--audio ..]
                                  always-on rolling buffer (30s segments, old ones auto-deleted)
  sentry-save --minutes N [--out] export the last N minutes to mp4 (stream copy, instant)
  sentry-status / sentry-stop
  voice-start [--content audio|audio+screen] [--mode conversation|any] [--hangover 90] [--min-speech 2] [--fps 1] [--max-height 720]
                                  watch mic + system sound; on real conversation auto-record
                                  call_*.mp4 (audio + low-fps screen) and call_*.m4a (for transcription)
  voice-status / voice-stop

Only one recording at a time. State: %TEMP%\\screenrec_state.json.
Default output folder: ~/Videos (override with env SCREENREC_DIR).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

STATE = Path(tempfile.gettempdir()) / "screenrec_state.json"
STOPFLAG = Path(tempfile.gettempdir()) / "screenrec_stop.flag"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def out_dir() -> Path:
    d = Path(os.environ.get("SCREENREC_DIR") or Path.home() / "Videos")
    d.mkdir(parents=True, exist_ok=True)
    return d


def jprint(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def ffmpeg_path() -> str:
    from shutil import which
    p = which("ffmpeg")
    if p:
        return p
    # winget installs are not always on PATH of non-interactive shells
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for c in base.glob("*ffmpeg*/**/bin/ffmpeg.exe"):
        return str(c)
    raise RuntimeError("ffmpeg not found (winget install Gyan.FFmpeg)")


# ---------- monitors ----------
# Per-monitor-v2 DPI awareness gives true physical pixel geometry, which matches gdigrab's space.
def monitors() -> list[dict]:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
    res: list[dict] = []

    class RECT(ctypes.Structure):
        _fields_ = [("l", wintypes.LONG), ("t", wintypes.LONG), ("r", wintypes.LONG), ("b", wintypes.LONG)]

    cb_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)

    def cb(_h, _dc, rc, _lp):
        r = rc.contents
        dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(_h, 0, ctypes.byref(dx), ctypes.byref(dy))
        except Exception:
            pass
        res.append({"x": r.l, "y": r.t, "w": r.r - r.l, "h": r.b - r.t, "dpi": dx.value})
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, cb_t(cb), 0)
    # primary (origin 0,0) first, then left-to-right
    res.sort(key=lambda m: (not (m["x"] == 0 and m["y"] == 0), m["x"], m["y"]))
    for i, m in enumerate(res, 1):
        m["index"] = i
        m["primary"] = i == 1
    return res


def virtual_screen() -> dict:
    mons = monitors()
    x0 = min(m["x"] for m in mons)
    y0 = min(m["y"] for m in mons)
    return {"x": x0, "y": y0, "w": max(m["x"] + m["w"] for m in mons) - x0,
            "h": max(m["y"] + m["h"] for m in mons) - y0}


def resolve_target(target: str) -> dict:
    t = target.strip().lower()
    if t in ("all", "desktop"):
        return virtual_screen()
    m = re.fullmatch(r"monitor:(\d+)", t)
    if m:
        mons = monitors()
        i = int(m.group(1))
        if not 1 <= i <= len(mons):
            raise ValueError(f"monitor {i} not found; have {len(mons)}")
        return {k: mons[i - 1][k] for k in "xywh"}
    m = re.fullmatch(r"region:(-?\d+),(-?\d+),(\d+),(\d+)", t)
    if m:
        x, y, w, h = map(int, m.groups())
        return {"x": x, "y": y, "w": w, "h": h}
    raise ValueError("target must be all | monitor:N | region:X,Y,W,H")


def grab_args(r: dict) -> list[str]:
    return ["-f", "gdigrab", "-offset_x", str(r["x"]), "-offset_y", str(r["y"]),
            "-video_size", f"{r['w']}x{r['h']}"]


# ---------- audio / encoders ----------
# --audio: none | auto (= system + mic) | system | mic | <exact dshow device name>.
# auto/system/mic use WASAPI loopback / default mic and follow default-device changes (audiocap.py).
PIPE_MODES = ("auto", "both", "system", "mic")


def audio_inputs(mode: str) -> list[str]:
    if mode == "none":
        return []
    if mode in PIPE_MODES:
        return ["-f", "s16le", "-ar", "48000",         "-ac", "2",
                "-thread_queue_size", "512", "-i", "{AUDIO_URL}"]
    return ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={mode}"]


def check_audio(mode: str) -> None:
    if mode in PIPE_MODES:
        try:
            import audiocap  # noqa: F401
        except ImportError as e:
            raise RuntimeError(f"audio mode '{mode}' needs: pip install pyaudiowpatch numpy ({e})")


def audio_devices() -> list[str]:
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, creationflags=NOWIN, errors="replace")
    return re.findall(r'"([^"]+)" \(audio\)', p.stderr)


def pick_encoder(req: str) -> str:
    ff = ffmpeg_path()
    if req != "auto":
        return req
    enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True,
                         creationflags=NOWIN, errors="replace").stdout
    for e in ("nvenc", "qsv", "amf"):
        if f"hevc_{e}" in enc:
            t = subprocess.run([ff, "-hide_banner", "-loglevel", "quiet", "-f", "lavfi", "-i", "color=s=1280x720:d=0.2",
                                "-pix_fmt", "yuv420p", "-c:v", f"hevc_{e}", "-f", "null", "-"],
                               creationflags=NOWIN)
            if t.returncode == 0:
                return e
    return "x265"


VENC = {
    "qsv": ["-c:v", "hevc_qsv", "-global_quality", "26", "-preset", "medium", "-look_ahead", "0", "-bf", "0"],
    "nvenc": ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "28", "-b:v", "0"],
    "amf": ["-c:v", "hevc_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "26", "-qp_p", "28"],
    "x265": ["-c:v", "libx265", "-preset", "veryfast", "-crf", "28"],
    "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
}


# ---------- state ----------
def load_state() -> dict | None:
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return None


def alive(pid: int) -> bool:
    if not pid:
        return False
    p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                       capture_output=True, text=True, creationflags=NOWIN)
    return f'"{pid}"' in p.stdout


def file_info(path: str) -> dict:
    p = Path(path)
    return {"path": str(p), "exists": p.exists(), "size_mb": round(p.stat().st_size / 1048576, 2) if p.exists() else 0}


# ---------- commands ----------
def cmd_devices(_a) -> dict:
    return {"monitors": monitors(), "virtual_screen": virtual_screen(),
            "audio_devices": audio_devices(), "default_encoder": pick_encoder("auto"),
            "output_dir": str(out_dir())}


def cmd_start(a) -> dict:
    st = load_state()
    if st and alive(st.get("worker_pid", 0)):
        return {"ok": False, "error": "already recording", **file_info(st["out"])}
    region = resolve_target(a.target)
    check_audio(a.audio)
    # even dimensions required for yuv420p
    region["w"] -= region["w"] % 2
    region["h"] -= region["h"] % 2
    enc = pick_encoder(a.encoder)
    ext = a.format
    out = a.out or str(out_dir() / f"rec_{datetime.now():%Y%m%d_%H%M%S}.{ext}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    vf = [f"fps={a.fps}"]
    if a.max_height:
        vf.append(f"scale=-2:min(ih\\,{a.max_height}):flags=bicubic")
    vf.append("format=yuv420p")
    cmd = [ffmpeg_path(), "-hide_banner", "-y", "-thread_queue_size", "1024",
           *grab_args(region), "-framerate", str(a.fps), "-draw_mouse", "1", "-i", "desktop"]
    cmd += audio_inputs(a.audio)
    cmd += ["-vf", ",".join(vf), *VENC[enc], "-tag:v", "avc1" if enc == "x264" else "hvc1"]
    if a.audio != "none":
        cmd += ["-c:a", "aac", "-b:a", "128k", "-af", "aresample=async=1"]
    if ext == "mp4":
        cmd += ["-movflags", "+faststart"]
    if a.max_seconds:
        cmd += ["-t", str(a.max_seconds)]
    cmd.append(out)

    STOPFLAG.unlink(missing_ok=True)
    log = Path(tempfile.gettempdir()) / "screenrec_ffmpeg.log"
    worker = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_worker", str(log), str(STOPFLAG), "-", "0", a.audio, *cmd],
                              creationflags=DETACHED, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    STATE.write_text(json.dumps({"worker_pid": worker.pid, "out": out, "started": time.time(),
                                 "encoder": enc, "region": region, "log": str(log)}))
    time.sleep(2.0)
    if not alive(worker.pid):
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        STATE.unlink(missing_ok=True)
        return {"ok": False, "error": "ffmpeg exited immediately", "log_tail": tail}
    return {"ok": True, "recording": True, "out": out, "encoder": enc, "region": region,
            "audio": a.audio, "fps": a.fps, "hint": "call stop to finalize the file"}


def prune_segments(d: str, keep_s: int) -> None:
    cutoff = time.time() - keep_s
    for f in Path(d).glob("sentry_*.ts"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def cmd_worker(a) -> None:
    """Internal: owns ffmpeg; sends 'q' when the stop flag appears; optionally prunes old segments
    and feeds live audio (system loopback / mic that follows default-device changes) via TCP."""
    flag = Path(a.flag)
    last_prune = 0.0
    cmd = list(a.ffmpeg_cmd)
    feeder = None
    if a.audio_mode in PIPE_MODES:
        import socket
        import audiocap
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cmd = [c.replace("{AUDIO_URL}", f"tcp://127.0.0.1:{srv.getsockname()[1]}") for c in cmd]
        feeder = audiocap.Feeder(a.audio_mode, srv, Path(a.log).with_suffix(".audio.json"))
        feeder.start()
    with open(a.log, "wb") as lf:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=lf, stderr=lf, creationflags=NOWIN)
        while p.poll() is None:
            if a.keep_s and time.time() - last_prune > 15:
                prune_segments(a.prune_dir, a.keep_s)
                last_prune = time.time()
            if flag.exists():
                try:
                    p.stdin.write(b"q")
                    p.stdin.flush()
                except Exception:
                    pass
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
                break
            time.sleep(0.3)


def cmd_stop(_a) -> dict:
    st = load_state()
    if not st:
        return {"ok": False, "error": "no recording state"}
    if alive(st["worker_pid"]):
        STOPFLAG.write_text("stop")
        for _ in range(100):
            if not alive(st["worker_pid"]):
                break
            time.sleep(0.3)
        else:
            return {"ok": False, "error": "worker did not exit within 30s", **file_info(st["out"])}
    STOPFLAG.unlink(missing_ok=True)
    STATE.unlink(missing_ok=True)
    return {"ok": True, "recording": False, "duration_s": round(time.time() - st["started"], 1),
            **file_info(st["out"])}


def cmd_status(_a) -> dict:
    st = load_state()
    if not st or not alive(st["worker_pid"]):
        return {"recording": False, "stale_state": bool(st)}
    return {"recording": True, "elapsed_s": round(time.time() - st["started"], 1),
            "encoder": st["encoder"], "audio_sources": _audio_info("screenrec_ffmpeg"), **file_info(st["out"])}


def _audio_info(stem: str) -> dict | None:
    try:
        return json.loads((Path(tempfile.gettempdir()) / f"{stem}.audio.json").read_text())
    except Exception:
        return None


SENTRY_STATE = Path(tempfile.gettempdir()) / "screenrec_sentry.json"
SENTRY_FLAG = Path(tempfile.gettempdir()) / "screenrec_sentry_stop.flag"
SEG_S = 30


def sentry_dir() -> Path:
    d = Path(os.environ.get("SCREENREC_SENTRY_DIR") or Path(tempfile.gettempdir()) / "screenrec_sentry")
    d.mkdir(parents=True, exist_ok=True)
    return d


def sentry_state() -> dict | None:
    try:
        return json.loads(SENTRY_STATE.read_text())
    except Exception:
        return None


def cmd_sentry_start(a) -> dict:
    st = sentry_state()
    if st and alive(st.get("worker_pid", 0)):
        return {"ok": False, "error": "sentry already running", "dir": st["dir"]}
    region = resolve_target(a.target)
    check_audio(a.audio)
    region["w"] -= region["w"] % 2
    region["h"] -= region["h"] % 2
    enc = pick_encoder(a.encoder)
    d = sentry_dir()
    for f in d.glob("sentry_*.ts"):
        f.unlink(missing_ok=True)
    vf = [f"fps={a.fps}"]
    if a.max_height:
        vf.append(f"scale=-2:min(ih\\,{a.max_height}):flags=fast_bilinear")
    vf.append("format=yuv420p")
    cmd = [ffmpeg_path(), "-hide_banner", "-y", "-thread_queue_size", "1024",
           *grab_args(region), "-framerate", str(a.fps), "-draw_mouse", "1", "-i", "desktop"]
    cmd += audio_inputs(a.audio)
    cmd += ["-vf", ",".join(vf), *VENC[enc], "-g", str(a.fps * 2), "-tag:v", "avc1" if enc == "x264" else "hvc1"]
    if a.audio != "none":
        cmd += ["-c:a", "aac", "-b:a", "96k", "-af", "aresample=async=1"]
    cmd += ["-f", "segment", "-segment_time", str(SEG_S), "-segment_format", "mpegts",
            "-reset_timestamps", "1", "-strftime", "1", str(d / "sentry_%Y%m%d_%H%M%S.ts")]

    SENTRY_FLAG.unlink(missing_ok=True)
    log = Path(tempfile.gettempdir()) / "screenrec_sentry_ffmpeg.log"
    keep = a.buffer_minutes * 60 + 2 * SEG_S
    worker = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_worker", str(log),
                               str(SENTRY_FLAG), str(d), str(keep), a.audio, *cmd],
                              creationflags=DETACHED, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    SENTRY_STATE.write_text(json.dumps({"worker_pid": worker.pid, "dir": str(d), "started": time.time(),
                                        "buffer_minutes": a.buffer_minutes, "encoder": enc, "region": region}))
    time.sleep(2.0)
    if not alive(worker.pid):
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        SENTRY_STATE.unlink(missing_ok=True)
        return {"ok": False, "error": "ffmpeg exited immediately", "log_tail": tail}
    return {"ok": True, "sentry": True, "buffer_minutes": a.buffer_minutes, "encoder": enc,
            "fps": a.fps, "region": region, "dir": str(d)}


def cmd_sentry_stop(_a) -> dict:
    st = sentry_state()
    if not st:
        return {"ok": False, "error": "sentry not running"}
    if alive(st["worker_pid"]):
        SENTRY_FLAG.write_text("stop")
        for _ in range(100):
            if not alive(st["worker_pid"]):
                break
            time.sleep(0.3)
    SENTRY_FLAG.unlink(missing_ok=True)
    SENTRY_STATE.unlink(missing_ok=True)
    return {"ok": True, "sentry": False, "note": "buffer segments kept until next sentry-start",
            "dir": st["dir"]}


def _segments(d: Path) -> list[Path]:
    return sorted(d.glob("sentry_*.ts"))


def cmd_sentry_status(_a) -> dict:
    st = sentry_state()
    run = bool(st and alive(st["worker_pid"]))
    segs = _segments(sentry_dir())
    size = sum(f.stat().st_size for f in segs) / 1048576
    span = (segs[-1].stat().st_mtime - _seg_start(segs[0])) / 60 if segs else 0
    return {"sentry": run, "buffered_minutes": round(span, 1), "segments": len(segs),
            "audio_sources": _audio_info("screenrec_sentry_ffmpeg") if run else None,
            "buffer_mb": round(size, 1), "buffer_minutes_max": st["buffer_minutes"] if st else None}


def _seg_start(p: Path) -> float:
    return datetime.strptime(p.stem, "sentry_%Y%m%d_%H%M%S").timestamp()


def cmd_sentry_save(a) -> dict:
    """Export the last N minutes of the buffer to an mp4 (stream copy, no re-encode, near-instant)."""
    d = sentry_dir()
    now = time.time()
    win_start = now - a.minutes * 60
    segs = [f for f in _segments(d) if f.stat().st_mtime >= win_start]
    if not segs:
        return {"ok": False, "error": "no buffered footage in that window (is sentry running?)"}
    offset = max(0.0, win_start - _seg_start(segs[0]))
    out = a.out or str(out_dir() / f"sentry_{datetime.now():%Y%m%d_%H%M%S}.mp4")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    lst = Path(tempfile.gettempdir()) / "screenrec_sentry_concat.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in segs))
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(lst)]
    if offset > 0:
        cmd += ["-ss", f"{offset:.2f}"]
    cmd += ["-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=NOWIN, errors="replace")
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr[-800:]}
    return {"ok": True, "minutes_requested": a.minutes, "segments": len(segs), **file_info(out)}


VOICE_STATE = Path(tempfile.gettempdir()) / "screenrec_voice.json"
VOICE_FLAG = Path(tempfile.gettempdir()) / "screenrec_voice_stop.flag"


def _voice_state() -> dict | None:
    try:
        st = json.loads(VOICE_STATE.read_text())
        return st if alive(st["pid"]) else None
    except Exception:
        return None


def cmd_voice_start(a) -> dict:
    """Watch for real conversations and auto-record them (audio + 1-2 fps screenshots)."""
    if _voice_state():
        return {"ok": False, "error": "voice watcher already running"}
    try:
        import voicewatch  # noqa: F401
    except ImportError as e:
        return {"ok": False, "error": f"needs: pip install webrtcvad-wheels pyaudiowpatch numpy ({e})"}
    VOICE_FLAG.unlink(missing_ok=True)
    cfg = {"mode": a.mode, "hangover_s": a.hangover, "min_speech_s": a.min_speech, "fps": a.fps,
           "max_height": a.max_height, "target": a.target, "out_dir": str(out_dir()), "max_minutes": a.max_minutes,
           "content": a.content}
    w = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_voiceworker", json.dumps(cfg)],
                         creationflags=DETACHED, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    VOICE_STATE.write_text(json.dumps({"pid": w.pid, "started": time.time(), **cfg}))
    time.sleep(2.0)
    if not alive(w.pid):
        VOICE_STATE.unlink(missing_ok=True)
        return {"ok": False, "error": "voice watcher exited immediately"}
    return {"ok": True, "watching": True, **cfg}


def cmd_voice_worker(a) -> None:
    import voicewatch
    cfg = json.loads(a.cfg)

    def start(out):
        return cmd_start(argparse.Namespace(target=cfg["target"], audio="auto", fps=cfg["fps"],
                                            max_height=cfg["max_height"], encoder="auto", format="mp4",
                                            out=out, max_seconds=0))

    def running():
        st = load_state()
        return bool(st and alive(st.get("worker_pid", 0)))

    voicewatch.run(cfg, start, lambda: cmd_stop(None), running, VOICE_FLAG)


def cmd_voice_stop(_a) -> dict:
    st = _voice_state()
    if not st:
        VOICE_STATE.unlink(missing_ok=True)
        return {"ok": False, "error": "voice watcher not running"}
    VOICE_FLAG.write_text("stop")
    for _ in range(150):
        if not alive(st["pid"]):
            break
        time.sleep(0.3)
    VOICE_FLAG.unlink(missing_ok=True)
    VOICE_STATE.unlink(missing_ok=True)
    return {"ok": True, "watching": False}


def cmd_voice_status(_a) -> dict:
    st = _voice_state()
    if not st:
        return {"watching": False}
    try:
        info = json.loads((Path(tempfile.gettempdir()) / "screenrec_voice.status.json").read_text())
    except Exception:
        info = {}
    return {**info, "watching": True}


def cmd_screenshot(a) -> dict:
    r = resolve_target(a.target)
    out = a.out or str(out_dir() / f"shot_{datetime.now():%Y%m%d_%H%M%S}.png")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *grab_args(r),
                        "-draw_mouse", "0", "-i", "desktop", "-frames:v", "1", out],
                       capture_output=True, text=True, creationflags=NOWIN, errors="replace")
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr[-800:]}
    return {"ok": True, **file_info(out), "region": r}


def cmd_list(a) -> dict:
    files = sorted([p for p in [*out_dir().glob("rec_*.*"), *out_dir().glob("call_*.*")]
                    if p.suffix in (".mp4", ".mkv", ".m4a")],
                   key=lambda p: p.stat().st_mtime, reverse=True)[: a.limit]
    return {"dir": str(out_dir()), "recordings": [
        {**file_info(str(p)), "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        for p in files]}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    s = sub.add_parser("start")
    s.add_argument("--target", default="all")
    s.add_argument("--audio", default="auto")
    s.add_argument("--fps", type=int, default=30)
    s.add_argument("--max-height", type=int, default=1080)
    s.add_argument("--encoder", default="auto", choices=["auto", *VENC])
    s.add_argument("--format", default="mp4", choices=["mp4", "mkv"])
    s.add_argument("--out")
    s.add_argument("--max-seconds", type=int, default=0)
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("screenshot")
    s.add_argument("--target", default="all")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_screenshot)
    s = sub.add_parser("list")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("sentry-start")
    s.add_argument("--target", default="all")
    s.add_argument("--audio", default="auto")
    s.add_argument("--fps", type=int, default=5)
    s.add_argument("--max-height", type=int, default=1080)
    s.add_argument("--encoder", default="auto", choices=["auto", *VENC])
    s.add_argument("--buffer-minutes", type=int, default=30)
    s.set_defaults(fn=cmd_sentry_start)
    sub.add_parser("sentry-stop").set_defaults(fn=cmd_sentry_stop)
    sub.add_parser("sentry-status").set_defaults(fn=cmd_sentry_status)
    s = sub.add_parser("sentry-save")
    s.add_argument("--minutes", type=float, default=10)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_sentry_save)
    s = sub.add_parser("voice-start")
    s.add_argument("--mode", default="conversation", choices=["conversation", "any"])
    s.add_argument("--hangover", type=int, default=90, help="seconds of silence before stopping")
    s.add_argument("--min-speech", type=float, default=2.0, help="speech seconds per stream (in 20s) to trigger")
    s.add_argument("--fps", type=float, default=1)
    s.add_argument("--max-height", type=int, default=720)
    s.add_argument("--target", default="all")
    s.add_argument("--max-minutes", type=int, default=240)
    s.add_argument("--content", choices=["audio", "audio+screen"], default="audio",
                   help="audio = only call_*.m4a (default); audio+screen also records call_*.mp4 at low fps")
    s.set_defaults(fn=cmd_voice_start)
    sub.add_parser("voice-stop").set_defaults(fn=cmd_voice_stop)
    sub.add_parser("voice-status").set_defaults(fn=cmd_voice_status)
    w = sub.add_parser("_voiceworker")
    w.add_argument("cfg")
    w.set_defaults(fn=cmd_voice_worker)
    w = sub.add_parser("_worker")
    w.add_argument("log")
    w.add_argument("flag")
    w.add_argument("prune_dir")
    w.add_argument("keep_s", type=int)
    w.add_argument("audio_mode")
    w.add_argument("ffmpeg_cmd", nargs=argparse.REMAINDER)
    w.set_defaults(fn=cmd_worker)
    return ap


def main() -> int:
    a = build_parser().parse_args()
    try:
        r = a.fn(a)
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    if r is not None:
        jprint(r)
    return 0 if (r is None or r.get("ok", True)) else 1


if __name__ == "__main__":
    sys.exit(main())
