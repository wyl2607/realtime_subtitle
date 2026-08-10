# Windows runbook (after package layout)

Repo is already on GitHub `master` (`f872c9e`+). Do this on the Windows machine.

## A. First-time install (new PC)

```powershell
# 1) Clone to an ASCII-only path (required)
cd C:\
git clone https://github.com/wyl2607/realtime_subtitle.git
cd C:\realtime_subtitle

# 2) Install (venv, GPU tier, Ollama, desktop shortcuts)
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# China network:
# powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror
```

If Ollama is missing, accept the winget prompt (`Y`).

Desktop folder: **德语直播实时字幕**  
- 启动字幕.bat / 停止 / 暂停继续 / 更新 / 卸载

## B. Existing install — update to latest layout

Old clones still work only after **update + shortcut refresh**.

```powershell
cd C:\realtime_subtitle   # your real clone path

# Prefer the new script path (after pull the old root scripts are gone)
git pull

powershell -ExecutionPolicy Bypass -File scripts\windows\update_subtitles.ps1

# Refresh desktop .bat paths (points to scripts\windows\*.ps1)
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

`install.ps1` is idempotent: it will not wipe `config_local.py` / transcripts.

Then:

1. **停止字幕.bat** (if running)
2. **启动字幕.bat**

## C. Daily use

| Action | How |
|--------|-----|
| Start | Desktop **启动字幕.bat** |
| Stop | **停止字幕.bat** |
| Pause | **暂停继续字幕.bat** or `Ctrl+Alt+P` |
| Update later | **更新字幕.bat** |

Manual start (debug):

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -u main.py
```

Logs: `subtitle.log`, `subtitle.err.log`, `logs\`.

## D. Smoke checks after update

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -c "from realtime_subtitle import config, version_string; print(version_string(), config.OLLAMA_MODEL)"
venv\Scripts\python -c "import torch; from realtime_subtitle.translate import translator_queue; translator_queue._ensure_ml_deps(); print('SMOKE_OK')"
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
```

## E. Still optional / watch

| Item | Notes |
|------|--------|
| Desktop shortcut path | Must call `scripts\windows\...ps1` (re-run install if still old) |
| `config_local.py` | Stays at **repo root** (not inside the package) |
| CI Windows job | First green run on GitHub Actions after push — check Actions tab |
| Standalone GUI tests | `tests\test_hittest.py` etc. still manual (real windows) |
| Issue #28 | Closed — package layout done |

## F. Common failures

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: realtime_subtitle` | Run from repo root; re-pull; venv must be this clone’s `venv` |
| `import config` fails in old notes | Use `from realtime_subtitle import config` |
| No Chinese / no translation | Start Ollama; `ollama list` vs `config.OLLAMA_MODEL` |
| Shortcut “file not found” | Re-run `scripts\windows\install.ps1` to regenerate `.bat` |
| Non-ASCII install path | Move clone to e.g. `C:\realtime_subtitle` |
