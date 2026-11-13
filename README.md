# ScreenRecorder

Lightweight Windows screen & call recorder for **humans and AI agents**: tiny HEVC files, audio that follows your default devices, a Tesla-style "sentry" rolling buffer, and an automatic **call recorder** that triggers on real conversation and produces transcription-ready audio.

- **Small files**: 2 monitors, 20+ min ≈ 27 MB (HEVC, hardware encoder auto-detected, low-fps for calls).
- **Audio that never loses the source**: system sound (WASAPI loopback) + microphone, auto-following the Windows default devices.
- **Sentry**: always-on rolling buffer (default 30 min, ~1% CPU); export "the last 10 minutes" on demand.
- **Voice watch**: listens for conversational speech (WebRTC VAD, not just loudness) on mic *and* system audio, then records the call.
- **Three front-ends, one engine**: tray GUI, JSON CLI, MCP server.

## Install

```powershell
winget install Gyan.FFmpeg            # ffmpeg on PATH
pip install -r requirements.txt       # numpy, pyaudiowpatch, webrtcvad-wheels, mcp
```

Windows 10/11 and Python 3.10+. Optional: build the windowless launcher `ScreenRecorder.exe` with `powershell -File launcher\build.ps1` (or just use `Recorder.bat`).

## GUI

Double-click `ScreenRecorder.exe` / `Recorder.bat` (no console window). Close (X) or minimize hides it to the tray; everything keeps running. Double-click the tray icon to open, right-click for quick actions. Start hidden at login: put a shortcut to `ScreenRecorder.exe -Tray` in `shell:startup`.

| Tray icon | Meaning |
|-----------|---------|
| Grey | idle |
| Blue with lens | sentry buffer running |
| Green with mic | voice watch listening |
| Blue/green split | sentry + voice watch |
| Red | recording a call |

Windows 11 may hide new tray icons behind `^`; the app marks itself "always show", otherwise drag the icon onto the taskbar.

## CLI (every command prints JSON)

```powershell
python screenrec.py devices
python screenrec.py start --target monitor:2 --fps 30 --max-height 1080 --max-seconds 600
python screenrec.py status
python screenrec.py stop                      # path / size / duration
python screenrec.py screenshot --target region:0,0,800,600 --out shot.png
python screenrec.py list --limit 5
```

- `--target`: `all` | `monitor:N` (1 = primary) | `region:X,Y,W,H`
- `--audio auto` (default) | `system` | `mic` | `none` | exact dshow name
- `--max-height 1080` (`0` = original), `--format mp4|mkv`, `--encoder auto|qsv|nvenc|amf|x265|x264`
- Recording runs in a detached worker and survives the caller. **Always `stop`**, or set `--max-seconds`.
- Output: `~/Videos` (override `SCREENREC_DIR`). Exit code 1 + `{"ok": false, "error": ...}` on failure.

## Sentry (rolling buffer)

```powershell
python screenrec.py sentry-start --buffer-minutes 30      # 5 fps, 1080p max
python screenrec.py sentry-status
python screenrec.py sentry-save --minutes 10 --out incident.mp4
python screenrec.py sentry-stop
```

30 s segments, old ones deleted automatically; export is a stream copy (instant). Cost: ~0.25 core and ~6 MB/min at 5 fps. Buffer in `%TEMP%\screenrec_sentry` (`SCREENREC_SENTRY_DIR`); it does not survive a reboot.

## Voice watch (auto-record calls)

```powershell
python screenrec.py voice-start [--content audio|audio+screen] [--mode conversation|any] [--hangover 90] [--min-speech 2]
python screenrec.py voice-status
python screenrec.py voice-stop
```

- **`--content audio`** (default): only `call_*.m4a` — stereo 16 kHz, **left = you, right = others**, includes ~20 s *before* the trigger. Ready for Whisper & co (~8 MB / 17 min).
- **`--content audio+screen`**: also `call_*.mp4` (screen at 1 fps + audio).
- **`--mode conversation`** (default): needs speech on both sides within 20 s. `any`: either side (use if you stay muted in meetings).
- Stops after `--hangover` seconds of silence (default 90) or `--max-minutes` (240).
- Limits: without a headset the microphone hears the speakers, so one-sided audio can look like a conversation. Does not trigger while a manual recording runs.

## MCP

```powershell
claude mcp add screenrecorder -- python "<path>\mcp_server.py"
```

Or in any MCP client: `{"command": "python", "args": ["<abs path>\\mcp_server.py"]}`.

Tools: `screenrec_devices`, `screenrec_start`, `screenrec_stop`, `screenrec_status`, `screenrec_screenshot`, `screenrec_list`, `screenrec_sentry_start|save|status|stop`, `screenrec_voice_start|status|stop`.

## Files

| File | Role |
|------|------|
| `screenrec.py` | engine + CLI |
| `audiocap.py` | audio sources that follow default devices |
| `voicewatch.py` | speech-triggered call recorder |
| `mcp_server.py` | MCP wrapper |
| `recgui.ps1`, `Recorder.bat/.vbs`, `launcher/` | tray GUI and windowless launcher |
| `rec-cli.ps1` | simple console recorder |

## License

MIT
