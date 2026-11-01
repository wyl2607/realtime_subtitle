# Realtime Subtitle

**Live subtitles for Windows & macOS** — capture system audio, recognize speech, translate, and show a bilingual always-on-top overlay. Default to fully local; audio is only sent to the cloud recognition service when a Groq key is configured in the program settings. Current version **v3.2.0**.

> **Privacy commitment & off-device paths:**
>
> By default, recognition and translation run entirely locally; no audio or transcript is sent to any cloud service. There are only two optional paths where audio leaves your machine, and both require explicit configuration:
> 1. **Your own compute node:** audio is sent to a machine within your own Tailscale network (`100.64.0.0/10`), never passing through third parties;
> 2. **Cloud Groq (third-party):** audio is sent to Groq's cloud recognition service only if a Groq key is set in the program settings. Does not read environment variables (preventing unintended uploads from system-wide keys); cloud recognition can be disabled with one click in the settings panel or via CLI.
>
> The interface status line clearly indicates the current recognition mode and source: `"云端·Groq"` / `"混合·<node-name>"` / `"本机"`.
> When using a network proxy, the proxy can see the uploaded **raw audio** fragments (WAV).
> Regarding third-party data retention: according to the [official Groq data policy](https://console.groq.com/docs/your-data), Groq does not retain recognition requests by default; it may temporarily retain them for up to 30 days solely to investigate reliability issues or abuse. If concerned, enable Zero Data Retention (ZDR) under Data Controls in the Groq console.

> **This is enforced, not just documented.** At startup the app resolves
> `OLLAMA_BASE_URL` and refuses to run unless every resolved address is a loopback
> address — otherwise one typo in `config_local.py` would silently ship your transcripts
> off the machine while subtitles kept working normally. To use an Ollama on another
> machine, set `ALLOW_REMOTE_OLLAMA = True` in `config_local.py`.

> **One exception, and only if you click it:** the `🌐 Ask a stronger AI` button in the
> popups opens your system browser with a question built from the last few minutes of
> recognized source text (≤300 chars), sent to a web AI (grok.com by default,
> `config.AI_ANALYSIS_WEB_URL_TEMPLATE`). Nothing leaves the machine unless you press it,
> and even then a dialog first shows you **exactly what would be sent** (target host,
> total length, first 300 chars) and waits for your confirmation — set
> `AI_WEB_CONFIRM = False` to skip it. Set that template to an empty string to remove the
> button entirely. Note this app captures **all system audio**, which may include voice calls.

[中文](README.md) · [Deutsch](README.de.md) · [Repository layout](docs/STRUCTURE.md) · [Windows runbook](docs/en/WINDOWS-RUNBOOK.md)

```text
System audio ──WASAPI loopback──▶ Faster-Whisper (CUDA or CPU)
                                      │ local-agreement streaming
                                      ▼
                               Source text ──▶ Ollama (local LLM) translation
                                      │
                                      ▼
                         Overlay: source first + draft + final bilingual lines
```

## Platform status

As of 2026-10-25. The current release for Windows / macOS is v3.2.0 (2026-10-25).

Stage legend: ✅ stable · 🟢 usable · 🧪 experimental · ⬜ not started.

| Platform | Form | Stage | Available now | Still missing |
|---|---|---|---|---|
| Windows | Desktop app | ✅ Stable | Local recognition and translation; three-tier automatic routing from cloud (Groq) → computer node → local; the status line shows the current source. Full pytest on a real Windows machine (RTX 4070): 1269 passed, 0 failed; smoke test and 150% HiDPI check passed | Occasionally splits a sentence early (shown as two lines; translation unaffected) |
| macOS | Desktop app + rslite (native Swift) | 🟢 Usable | Local recognition and translation; three-tier automatic routing from cloud → computer node → local. Cloud segment end → on screen median ~1.1 s, hybrid-mode median refine latency 1.47 s | Still distributed as source (rslite .app packaging is planned for v3.3); rslite release build with Swift 6.3 hangs on CI, under investigation |
| Android | Native app (separate private repo) | 🧪 Experimental | v0.4.0-quality released (old architecture); the new architecture is merged into main: on-device recognition with live German drafts, cloud tier, node tier, automatic switching, Keystore-encrypted key storage, all unit tests passing | New architecture not yet verified on a real device; no new release yet |
| iOS | Native app (prototype) | 🧪 Experimental | Screen-recording broadcast extension captures audio from other apps, or use the microphone directly; three-tier cloud → node → local routing; pairing via QR code / `rsnode://pair`; today's subtitle history | Not yet run on a real device; build and install yourself with Xcode; display is picture-in-picture only, no desktop-style overlay |
| Linux | — | ⬜ Not started | — | No plans |

## Features

- **Source text first** — partial recognition appears immediately (grey = still revisable); translation follows
- **Draft translation** — mid-sentence draft in light blue italic; replaced by the final line when ready
- **Local-agreement streaming ASR** — prefix-stable commits, fewer fragment duplicates (ported from [whisper_streaming](https://github.com/ufal/whisper_streaming), MIT)
- **Glossary** — political and domain terms in `config.py` (`GLOSSARY`)
- **Hallucination filter** — drops typical TV-subtitle ghost phrases on silence/music
- **GPU preemption friendly** — under game load, subtitles lag instead of dropping forever
- **Resizable overlay** — drag edges/corners; size and position persist across restarts
- **Word lookup** — click a German word for lemma / POS / meaning (local LLM)
- **Bidirectional (DE↔ZH)** — `Ctrl+Alt+L` cycles the *language pair*, so Chinese
  audio gets German subtitles. Optional auto-detect (`AUTO_DETECT_LANGUAGE`, off by
  default) switches for you after it sees the same new language 3 times in a row;
  while it is confirming, new subtitles are held back (`LANGUAGE_HOLD_OUTPUT`, on by
  default) — dropped if the switch happens (they were decoded with the old language),
  released unchanged if it doesn't. The few seconds before the first detection (up to
  one detection interval) can still show garbage.
- **Click-through mode** — `Ctrl+Alt+M` so the overlay ignores mouse hits over video/games
- **Cinema bar** — transparent Netflix-style caption strip at the bottom of fullscreen video (`Ctrl+Alt+C` / 🎞); always click-through so it never steals player clicks
- **TV fullscreen** — opaque large-type window on another screen (📺)
- **AI analysis** — 🤖 summarizes the last few minutes locally; 🌐 can open a web AI after an explicit confirm dialog
- **Daily transcript archive** — `transcripts/` (timestamp + source + translation), kept
  forever by default. Plain text, and this app captures **all** system audio: set
  `TRANSCRIPT_KEEP_DAYS = 30` to auto-prune, or `SAVE_TRANSCRIPT = False` to record nothing.
- **Download and subtitle batch** — Desktop `YouTube下载加字幕.bat` or
  `scripts\windows\download_subtitle.ps1` downloads up to 4K, transcribes locally,
  creates SRT files, translates one segment at a time with local Ollama, and writes a
  Chinese study guide. Chinese source becomes Chinese + German; German, English, and
  other source languages become source + Chinese.
- **Hotkeys** — pause, language cycle, performance mode, cinema bar (see Usage)

## Requirements

| | Recommended | Minimum |
|---|---|---|
| OS | Windows 10/11 64-bit | Windows 10/11 (**Windows only** — WASAPI loopback) |
| GPU | NVIDIA 8 GB+ VRAM | CPU-only works (higher latency), or use Windows Live Captions (see "Optional recognition sources") |
| RAM | 16 GB | 8 GB |
| Disk | ~10 GB (deps + models) | ~6 GB |
| Python | 3.11–3.13 | same |
| Other | [Ollama](https://ollama.com/) for translation | same |

## Install

Clone into an **ASCII-only path** (e.g. `C:\realtime_subtitle`). Desktop shortcuts embed absolute paths; non-ASCII user profiles (common on Chinese Windows) break the generated `.bat` files.

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# China mirror: powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror

# Root shim still works:
# powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer checks the path and Python, detects NVIDIA VRAM (or falls back to CPU), creates a venv, installs dependencies, guides Ollama setup, and writes five desktop shortcuts under **“德语直播实时字幕”**: start (which also updates), download-and-subtitle, stop, pause, uninstall.

If Ollama is missing, the installer offers **`winget install --id Ollama.Ollama -e`** (finds the binary via common paths; PATH need not refresh). Decline to open the download page instead.

First launch downloads the Whisper model (~1.6 GB).

**AI-assisted install:** ask an agent to clone this repo and follow [CLAUDE.md](CLAUDE.md) for hardware tiers and known pitfalls.

## macOS (Apple Silicon)

The macOS build supports **Apple Silicon only (M1 and later)**. Intel Macs are not supported. Recognition uses MLX on the Apple GPU, not CUDA; sentence translation prefers the built-in macOS Translation service (install.sh builds rstranslate; you must first download German and Simplified Chinese in System Settings → General → Language & Region → Translation Languages); it falls back to local Ollama when unavailable. Word lookup and 🤖 AI analysis still use Ollama. Audio and text stay on your machine.

Prerequisites:

```bash
brew install uv ollama
```

Install:

```bash
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
bash scripts/macos/install.sh
# Mainland China mirror: bash scripts/macos/install.sh --mirror
# Low disk space: bash scripts/macos/install.sh --lean
```

`--lean` uses the 4-bit quantized Whisper model. Accuracy is slightly lower (German WER 4.05% → 4.84%) and saves about 1.1 GB. The installer chooses a tier by memory:

| RAM | Whisper | Translation model |
|---|---|---|
| <12 GB | turbo-q4 | qwen3.5:2b |
| 12–31 GB | turbo | qwen3.5:4b |
| ≥32 GB | turbo | qwen3.5:9b |

Machine-specific settings are written to `config_local.py`; do not edit `config.py`. This is the same mechanism as on Windows. If memory gets tight while running, the app steps down in order of accuracy cost: first speech recognition goes from fp16 to 8-bit (same accuracy, about 1 s slower per update), then the translation model drops one tier (for example 4b → 2b), and only last does recognition go to 4-bit. It steps back up once pressure stays normal, but never above the tier chosen at install time.

macOS has no built-in system loopback capture. To capture system audio:

```bash
brew install blackhole-2ch
```

Then open Audio MIDI Setup, create a Multi-Output Device, tick both your speakers and BlackHole 2ch, and switch system output to that device. Without BlackHole, the app falls back to the default microphone and shows a prompt. The ⚙️ panel's "device name contains" field can select an input device.

Start / stop:

```bash
bash scripts/macos/start.sh
bash scripts/macos/stop.sh
```

`start.sh` checks GitHub for updates before each launch; skip once with `bash scripts/macos/start.sh --no-update` or set `RS_NO_UPDATE=1`.

Logs are `subtitle.log` / `subtitle.err.log`. Hotkeys are the same as Windows: `Ctrl+Alt+P/L/M/G/C`; on Mac, press the physical **Control + Option** keys.

Known differences: on Mac, subtitle refresh is about once every 2–3 seconds (M2 measured recognition at about 2.4 seconds per pass), slower than an NVIDIA GPU. Single-sentence translation is about 4 seconds with 4b and 1.6 seconds with 2b. Expected disk use: Python environment about 1.5 GB + Whisper 1.6 GB (`--lean`: 0.5 GB) + translation model 2.7–6.6 GB.

### rslite (experimental, native minimal Mac version)

A native small app using system recognition + system translation; no Python or Ollama needed; unplugged total power is only about 0.1 W above idle. Build: `cd macos-native && swift build -c release`; run: `.build/release/rslite` (UI mode). Full details in `macos-native/README.md`.

Package as a .app: `bash scripts/macos/build_rslite_app.sh` produces `dist/macos/rslite.app` (the same directory also has `rslite-<version>-macos-arm64.zip` and its `.sha256`). Drag `rslite.app` into Applications and double-click it; a「字·…」item appears in the menu bar and there is no Dock icon (LSUIElement). Requires macOS 27+, Apple Silicon, and Xcode (the Swift toolchain). The bundle is only ad-hoc signed and **not notarized** (no paid developer account): a .app built on this machine opens directly; a zip downloaded from elsewhere is blocked by Gatekeeper on first launch—since macOS 15, Control/right-click → Open no longer bypasses it, so go to System Settings → Privacy & Security and click "Open Anyway", or run `xattr -dr com.apple.quarantine /Applications/rslite.app` in Terminal. The first launch asks for「System Audio Recording」(to capture system sound via Process Tap); microphone mode asks for Microphone access. The system may ask again after every rebuild (ad-hoc signature). Running from source still works: `cd macos-native && swift build -c release`, then `.build/release/rslite`.

### Compute node (experimental)

Offload recognition to another Apple Silicon Mac or NVIDIA Windows machine inside the tailnet: this machine shows subtitles first, then the node's refined results replace them; if the node disconnects, subtitles continue and automatically fall back to local processing, reconnecting when the node returns. Install: Mac node `bash scripts/node/install_node.sh <ssh alias>`, Windows node `bash scripts/node/install_node_windows.sh <ssh alias>` (both run on Mac). Privacy boundary: node listens only on Tailscale (`100.64.0.0/10`), token-authenticated (the token is passed only via files), audio stays in memory (no disk), logs contain no transcript content; audio leaves this machine only to your own node. Both rslite and the Windows desktop subtitle program support compute nodes (see "Windows: Using compute nodes" below). Protocol details: `docs/protocol-v2.md`.

## Cloud recognition (optional)

- **Mac rslite**: read key from standard input via `rslite --set-groq-key`, clear key with `rslite --clear-groq-key`, and toggle cloud mode with `rslite --cloud on|off`.
- **Windows**: enter the Groq key into the password field under "Cloud Recognition" in the ⚙️ settings panel and toggle the switch to enable/disable.
- **Free tier limits**: 20 requests per minute, 7,200 seconds of audio per hour, 2,000 requests / 28,800 seconds per day, with each request billed at minimum 10 seconds.
- **Automatic failover**: when quota approaches limits, the system automatically falls back to your compute node or local recognition; when quota recovers, it switches back at a silence point.
- **Fallback order**: Cloud → Own compute node → Local.

## Optional recognition sources and subtitle export

All of these go into `config_local.py`; restart the subtitles afterwards.

- **Windows Live Captions (no GPU needed)**: `ASR_SOURCE = "livecaptions"`, then
  `venv\Scripts\python -m pip install uiautomation`. Speech recognition is done by the Live Captions feature built
  into Windows 11 (22H2+); the app reads its text and translates it with Ollama. Whisper is not loaded and the GPU
  is not used, so this suits machines without an NVIDIA card. Pick the recognition language under
  **Windows Settings → Accessibility → Captions** and keep it in line with the app's source language. The Live
  Captions window is minimized while the app runs (`LIVE_CAPTIONS_HIDE_WINDOW = False` keeps it visible) and
  restored on exit. Live Captions holds back the last few words until the next phrase, so the last sentence before
  a pause arrives late (a ~12 s fallback flushes it). If the requirements are not met (not Windows 11, uiautomation
  missing, Live Captions not found) the app falls back to Whisper.
- **SenseVoice (compute nodes, optional)**: `ASR_ENGINE = "sensevoice"` plus `pip install funasr` on the node.
  Only Chinese / English / Japanese / Korean / Cantonese — **no German**; other languages fall back to Whisper.
  Local streaming recognition stays on Whisper.
- **Per-session SRT / CSV**: on by default. Besides the daily txt, every run also writes a bilingual SRT (drop it
  into a video player) and a CSV (opens in Excel) under `transcripts\sessions\`. Turn off with
  `SAVE_SESSION_SRT = False` / `SAVE_SESSION_CSV = False`; `SAVE_TRANSCRIPT = False` disables them too.
  `TRANSCRIPT_KEEP_DAYS` applies to them as well.

## Windows: Using compute nodes

- Node switch is off by default.
- Deploy configuration from a Mac with `bash scripts/node/install_node_windows.sh <node> --client <windows-ssh-alias>` (or `install_node.sh` with the same arguments).
- If a machine is both a node and a client, it automatically skips itself.

## Update & uninstall

| Action | Command |
|---|---|
| Start (pulls first) | Desktop `启动字幕.bat` — pulls the latest, then launches; still launches if the pull fails |
| Update only | `scripts\windows\update_subtitles.ps1` (`git pull` + deps; no separate desktop entry any more) |
| Uninstall / free space | `scripts\windows\uninstall.ps1` (asks per component; default is keep) |
| Cache only | `scripts\windows\uninstall.ps1 -CleanCache` |

Personal files never go into git: `config_local.py`, window state, `transcripts/`.

<details>
<summary>Manual install (no scripts)</summary>

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
# Install Ollama: https://ollama.com/download
ollama pull qwen3.5:9b
venv\Scripts\python -u main.py
```
</details>

## Usage

| Action | How |
|---|---|
| Move overlay | Drag anywhere on the window |
| Resize | Drag edges / corners (larger window = more history) |
| Word lookup | Single-click a German word |
| Click-through | `Ctrl+Alt+M` |
| Pause / resume | `Ctrl+Alt+P` (works over fullscreen games) |
| Cycle language pair | `Ctrl+Alt+L` (de→zh / zh→de / en→zh; edit `config.LANGUAGE_PAIRS`) |
| Performance mode | `Ctrl+Alt+G` (lighter ASR + model; free GPU for games) |
| Cinema bar | `Ctrl+Alt+C` or 🎞 (needed when the video is already fullscreen) |
| TV fullscreen | 📺 on the overlay |
| AI analysis | 🤖 on the overlay |
| Session history | 📜 on the overlay |
| Settings | ⚙️ (timing, font size, line count, cinema bar — live) |
| Quit | ❌ or desktop stop shortcut |

### Download a video and create bilingual subtitles

Double-click the desktop `YouTube下载加字幕.bat`. If the clipboard contains a video URL it is used automatically; otherwise the console prompts for a URL, which can be pasted or typed. The default maximum is 2160p (4K). The pipeline extracts audio, runs the project’s local Faster-Whisper model, writes source and bilingual SRT files, and translates each subtitle independently with local Ollama so long videos do not accumulate duplicated context.

Files are written to `downloads\<video-id>\`:

- `<video-id>.<source>-<target>.bilingual.srt` — source first, then the translation; ready to import into a player
- `<video-id>.<source>-<target>.target.srt` — translation only (monolingual)
- `<video-id>.<source>.source.srt` — source-only SRT for review
- `<video-id>.<source>-<target>.learning.md` — Chinese overview, dialogue flow, expressions, and study steps

The script asks for the subtitle form (1 bilingual, the default; 2 translation only) and the target language (1 English, the default for Chinese videos; 2 German). Default targets: Chinese source → English, every other source → Chinese; an explicitly chosen target is never overridden by language detection. All three SRT variants are written every run, and the file names carry the language pair, so switching target or form never overwrites an earlier result and never re-runs ASR. The realtime pipeline's Chinese → German pair (`LANGUAGE_PAIRS`) is unchanged. Command-line use:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\download_subtitle.ps1 -Url "video URL"
# Add -NoSummary to skip the study guide
```

**Colours:** white = committed source · grey italic = provisional source tail · light-blue italic = draft translation · light grey = final translation.

## Configuration

Defaults live in [config.py](realtime_subtitle/config.py). Prefer overrides in **`config_local.py`** (gitignored):

```python
WHISPER_MODEL = "large-v3-turbo"   # or "medium" / "small"
OLLAMA_MODEL = "qwen3.5:9b"        # weaker machines: "qwen3.5:2b"
SOURCE_LANGUAGE = "de"
DRAFT_TRANSLATION = True
GLOSSARY = {...}
```

VRAM tiers and model picks are documented in [CLAUDE.md](CLAUDE.md).

## Repository layout

| Path | Role |
|---|---|
| `main.py` | Thin entrypoint (`python -u main.py`) |
| `realtime_subtitle/` | Package: `capture/`, `asr/`, `translate/`, `ui/`, `offline.py`, `app.py` |
| `scripts/windows/` | Install, start, stop, pause, update, uninstall, download-and-subtitle |
| `tests/` | Pytest + standalone GUI harnesses |
| `docs/` | Design specs, Chinese notes, structure guide |

See [docs/STRUCTURE.md](docs/STRUCTURE.md).

## Tests

```powershell
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
# GUI harnesses (not collected by pytest; they open real windows):
# Needs PYTHONPATH=. or it fails with ModuleNotFoundError: realtime_subtitle
$env:PYTHONPATH="."; venv\Scripts\python tests\test_hittest.py
```

## Troubleshooting

| Symptom | What to try |
|---|---|
| `cublas64_12.dll` missing | Reinstall `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` from `requirements.txt` |
| Source only, no translation | Start Ollama; `ollama pull` the model printed by `from realtime_subtitle import config; print(config.OLLAMA_MODEL)` |
| Frequent “GPU busy” | Settings → slower commit interval, or smaller Whisper model in `config_local.py` |
| No audio after headset change | Settings → device name contains… or `LOOPBACK_DEVICE_NAME` in `config_local.py` |
| Tiny UI on laptop | Windows display scaling; `Ctrl+scroll` for subtitle font size |
| Download or subtitle failure | Make sure `ffmpeg` is on PATH, start Ollama once, and reinstall dependencies to add `yt-dlp` |

Logs: `subtitle.log` / `subtitle.err.log` (and rotated files under `logs/`).

## Credits & license

- Originally inspired by [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle) (Apache-2.0); recognition pipeline rewritten
- Streaming agreement ideas from [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming) (MIT)
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

Licensed under [Apache-2.0](LICENSE).
