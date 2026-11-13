#!/usr/bin/env python3
"""Screen recorder MCP server (stdio). Thin wrapper over screenrec.py (CLI)."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import screenrec as sr  # noqa: E402

try:
    from fastmcp import FastMCP  # type: ignore
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP  # type: ignore

mcp = FastMCP(
    "screenrecorder",
    instructions=(
        "Record the Windows desktop to mp4/mkv and take screenshots. Workflow: "
        "screenrec_devices (monitors/audio names) -> screenrec_start -> do the work -> screenrec_stop "
        "(always stop, it finalizes the file). Targets: 'all', 'monitor:N', 'region:X,Y,W,H'. "
        "Audio default 'auto' = system sound + microphone, automatically following default-device changes; "
        "also 'system', 'mic' or 'none'. "
        "Use max_seconds as a safety net for unattended runs. screenrec_screenshot is the cheap way "
        "to capture a still PNG."
    ),
)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _run(fn, a):
    try:
        return fn(a)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(name="screenrec_devices")
def screenrec_devices() -> dict:
    """List monitors (index, geometry), audio input devices, default encoder and output folder."""
    return _run(sr.cmd_devices, None)


@mcp.tool(name="screenrec_start")
def screenrec_start(target: str = "all", audio: str = "auto", fps: int = 30, max_height: int = 1080,
                    encoder: str = "auto", format: str = "mp4", out: str = "", max_seconds: int = 0) -> dict:
    """Start background recording. target: all | monitor:N | region:X,Y,W,H. max_height 0 = original size.
    max_seconds 0 = until screenrec_stop. Only one recording at a time."""
    return _run(sr.cmd_start, _ns(target=target, audio=audio, fps=fps, max_height=max_height, encoder=encoder,
                                  format=format, out=out or None, max_seconds=max_seconds))


@mcp.tool(name="screenrec_stop")
def screenrec_stop() -> dict:
    """Stop the running recording gracefully; returns file path, size, duration."""
    return _run(sr.cmd_stop, None)


@mcp.tool(name="screenrec_status")
def screenrec_status() -> dict:
    """Whether a recording is running, elapsed seconds and current file size."""
    return _run(sr.cmd_status, None)


@mcp.tool(name="screenrec_screenshot")
def screenrec_screenshot(target: str = "all", out: str = "") -> dict:
    """Save a PNG screenshot (target: all | monitor:N | region:X,Y,W,H). Returns the file path."""
    return _run(sr.cmd_screenshot, _ns(target=target, out=out or None))


@mcp.tool(name="screenrec_voice_start")
def screenrec_voice_start(mode: str = "conversation", hangover_s: int = 90, min_speech_s: float = 2.0,
                          fps: float = 1, max_height: int = 720, target: str = "all",
                          content: str = "audio") -> dict:
    """Start the voice watcher: listens to system sound (others) + microphone (you) with a speech detector
    (not plain loudness). On a real conversation it auto-records call_*.mp4 (audio + screen at ~1 fps) and
    call_*.m4a (stereo: left = you, right = others, includes ~20 s before the trigger) ready for transcription;
    stops after hangover_s of silence. mode 'conversation' needs speech on both sides; 'any' = either side."""
    return _run(sr.cmd_voice_start, _ns(mode=mode, hangover=hangover_s, min_speech=min_speech_s, fps=fps,
                                        max_height=max_height, target=target, max_minutes=240,
                                        content=content))


@mcp.tool(name="screenrec_voice_status")
def screenrec_voice_status() -> dict:
    """Voice watcher state: listening/recording, speech seconds per stream, devices, calls saved (with m4a paths)."""
    return _run(sr.cmd_voice_status, None)


@mcp.tool(name="screenrec_voice_stop")
def screenrec_voice_stop() -> dict:
    """Stop the voice watcher (finalizes a call in progress)."""
    return _run(sr.cmd_voice_stop, None)


@mcp.tool(name="screenrec_sentry_start")
def screenrec_sentry_start(buffer_minutes: int = 30, fps: int = 5, target: str = "all", audio: str = "auto",
                           max_height: int = 1080, encoder: str = "auto") -> dict:
    """Start the always-on rolling buffer ('dashcam'): keeps only the last buffer_minutes on disk
    (~0.25 CPU core at 5 fps, ~6 MB/min). Independent of normal recording."""
    return _run(sr.cmd_sentry_start, _ns(target=target, audio=audio, fps=fps, max_height=max_height,
                                         encoder=encoder, buffer_minutes=buffer_minutes))


@mcp.tool(name="screenrec_sentry_save")
def screenrec_sentry_save(minutes: float = 10, out: str = "") -> dict:
    """Export the last `minutes` of the sentry buffer to an mp4 (stream copy, near-instant).
    Use after something happened ('capture the last 10 minutes')."""
    return _run(sr.cmd_sentry_save, _ns(minutes=minutes, out=out or None))


@mcp.tool(name="screenrec_sentry_status")
def screenrec_sentry_status() -> dict:
    """Whether sentry is running and how many minutes are currently buffered."""
    return _run(sr.cmd_sentry_status, None)


@mcp.tool(name="screenrec_sentry_stop")
def screenrec_sentry_stop() -> dict:
    """Stop the sentry buffer."""
    return _run(sr.cmd_sentry_stop, None)


@mcp.tool(name="screenrec_list")
def screenrec_list(limit: int = 10) -> dict:
    """List the most recent recordings in the output folder."""
    return _run(sr.cmd_list, _ns(limit=limit))


if __name__ == "__main__":
    mcp.run()
