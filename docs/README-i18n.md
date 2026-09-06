# README language policy (ZH / EN / DE)

Tracks [issue #27](https://github.com/wyl2607/realtime_subtitle/issues/27).

## Sources of truth

| Audience | File | Language |
|----------|------|----------|
| Public default (GitHub landing, Chinese friends) | [`README.md`](../README.md) | 中文 |
| English users | [`README.en.md`](../README.en.md) | English |
| German users | [`README.de.md`](../README.de.md) | Deutsch |
| Old Chinese URL | [`README.zh.md`](../README.zh.md) | stub → `README.md` |
| Deep install / hardware tiers / pitfalls | [`CLAUDE.md`](../CLAUDE.md) | 中文 (AI + advanced) |
| Layout | [`docs/STRUCTURE.md`](STRUCTURE.md) | English |
| Windows runbook | [`docs/WINDOWS-RUNBOOK.md`](WINDOWS-RUNBOOK.md) (EN), [`docs/zh/WINDOWS-RUNBOOK.md`](zh/WINDOWS-RUNBOOK.md) (ZH) | |

## Sync rules

1. **Same PR** when a user-visible feature changes:
   - hotkeys
   - install / update / uninstall commands
   - system requirements
   - important troubleshooting entries
2. Prefer **linking** long tables (VRAM tiers, full pitfall list) to `CLAUDE.md` instead of copying them three times.
3. Install commands must mention:
   - `scripts\windows\install.ps1` (canonical)
   - root `install.ps1` shim (backward compatible)
4. If one language cannot be updated in the same PR, open a follow-up issue and link it in the PR description.

## Checklist (copy into PR body when README-related)

```text
- [ ] README.md (ZH landing)
- [ ] README.en.md (EN)
- [ ] README.de.md (DE)
- [ ] README.zh.md still points at README.md
- [ ] CLAUDE.md paths still correct (if install/layout changed)
```
