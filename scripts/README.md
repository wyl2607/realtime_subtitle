# Scripts

## Windows (`scripts/windows/`)

| Script | Role |
|--------|------|
| `install.ps1` | One-shot setup (Python, venv, GPU tier, Ollama, desktop shortcuts) |
| `start_subtitles.ps1` | Start app (single-instance, Ollama health wait) |
| `stop_subtitles.ps1` | Graceful stop then force-kill fallback |
| `pause_subtitles.ps1` | Toggle pause flag without unloading models |
| `update_subtitles.ps1` | `git pull` + dependency sync |
| `uninstall.ps1` | Interactive cleanup (optional `-CleanCache`) |

All scripts resolve `$RepoRoot` two levels above this folder and run with the repository as working directory.

Root `install.ps1` is a **compatibility shim** that forwards to `scripts/windows/install.ps1`.
