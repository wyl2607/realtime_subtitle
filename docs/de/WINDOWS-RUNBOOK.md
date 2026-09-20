# Windows-Handbuch

[中文](../zh/WINDOWS-RUNBOOK.md) · [English](../en/WINDOWS-RUNBOOK.md) · **Deutsch**

Alles, was für den Betrieb nötig ist, sobald der Code auf GitHub `master` liegt.
Nur Windows — die Tonaufnahme nutzt WASAPI-Loopback.

## A. Erstinstallation (neuer PC)

```powershell
# 1) In einen reinen ASCII-Pfad klonen (zwingend)
cd C:\
git clone https://github.com/wyl2607/realtime_subtitle.git
cd C:\realtime_subtitle

# 2) Installieren (venv, GPU-Stufe, Ollama, Desktop-Verknüpfungen)
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# Netz in China:
# powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror
```

> **Warum nur ASCII?** Die erzeugten `.bat`-Dateien enthalten diesen Pfad fest, und unter
> `chcp 65001` zerlegt cmd Zeilen mit Nicht-ASCII-Zeichen falsch — der Starter ist dann
> schlicht kaputt. `install.ps1` bricht bei so einem Pfad bewusst ab.

Fehlt Ollama, die winget-Abfrage mit `Y` bestätigen. Beim ersten Start wird außerdem das
Whisper-Modell geladen (1–3 GB) — das ist normal und kein Hänger.

Im Desktop-Ordner **德语直播实时字幕** liegen danach fünf Einträge:

| Datei | Funktion |
|-------|----------|
| `启动字幕.bat` | **Der einzige, den man im Alltag braucht.** Holt die neueste Version und startet dann |
| `YouTube下载加字幕.bat` | Ein Video nachträglich untertiteln (nimmt auch lokale Dateien) |
| `停止字幕.bat` | Beenden |
| `暂停继续字幕.bat` | Pause / Weiter (wie `Ctrl+Alt+P`) |
| `卸载字幕.bat` | Deinstallieren; fragt pro Komponente, Vorgabe ist behalten |

## B. Bestehende Installation aktualisieren

```powershell
cd C:\realtime_subtitle   # der echte Klon-Pfad
powershell -ExecutionPolicy Bypass -File scripts\windows\update_subtitles.ps1

# Einmalig, damit das neue Fünf-Einträge-Layout auf dem Desktop ankommt:
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

`install.ps1` ist wiederholbar: `config_local.py`, Transkripte und Downloads werden nie
gelöscht. Entfernt werden lediglich ausgemusterte Desktop-Einträge
(`启动并更新字幕.bat`, `更新字幕.bat`, `下载并加字幕.bat`) — deren Verhalten steckt
jetzt in `启动字幕.bat`.

Beim Upgrade von einem Build vor v3.0.0 (PyQt5) bleiben die alten Qt-Pakete im venv
liegen, weil `pip install -r` nie etwas deinstalliert. Unschädlich, kostet nur Platz:

```powershell
venv\Scripts\python scripts\prune_venv.py --yes
```

## C. Täglicher Betrieb

| Aktion | Wie |
|--------|-----|
| Starten (aktualisiert vorher) | Desktop **启动字幕.bat**; startet auch, wenn das Update scheitert, und startet eine laufende Instanz nur dann neu, wenn wirklich neuer Code kam |
| Beenden | **停止字幕.bat** |
| Pause | **暂停继续字幕.bat** oder `Ctrl+Alt+P` |
| Video + zweisprachige SRT | **YouTube下载加字幕.bat**; Ergebnisse unter `downloads\<video-id>\` |
| Sprachpaar wechseln | `Ctrl+Alt+L` |
| Mausklicks durchreichen | `Ctrl+Alt+M` |
| Leistungsmodus (GPU fürs Spiel freigeben) | `Ctrl+Alt+G` |
| Kinoleiste | `Ctrl+Alt+C` — nötig, sobald das Video im Vollbild die Schaltflächen verdeckt |
| Nur aktualisieren, nicht starten | `scripts\windows\update_subtitles.ps1` (bewusst ohne Desktop-Eintrag) |

Manueller Start (zur Fehlersuche):

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -u main.py
# Video herunterladen und zweisprachig untertiteln:
venv\Scripts\python download_subtitle.py "https://www.youtube.com/watch?v=..."
```

Protokolle: `subtitle.log`, `subtitle.err.log`, `logs\`.

## D. Rauchtest nach einem Update

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -c "from realtime_subtitle import config, version_string; print(version_string(), config.OLLAMA_MODEL)"
venv\Scripts\python -c "import torch; from realtime_subtitle.translate import translator_queue; translator_queue._ensure_ml_deps(); print('SMOKE_OK')"
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
```

## E. Was dauerhaft gilt

| Punkt | Anmerkung |
|-------|-----------|
| `config_local.py` | Liegt immer im **Repo-Wurzelverzeichnis**, nie im Paket. Maschinenspezifisches gehört hierher — niemals `config.py` ändern, das erzeugt bei jedem Update Konflikte |
| `*.ps1` im Wurzelverzeichnis | Kompatibilitäts-Weiterleitungen für Installationen von vor 2026-08. **Nicht löschen** — alte Verknüpfungen zeigen darauf, auch die des Update-Skripts selbst; ohne sie hätten diese Nutzer keinen Weg zurück |
| Eigenständige GUI-Tests | `tests\test_hittest.py`, `test_resize_freedom.py`, `test_wordclick.py` öffnen echte Fenster; manuell mit `PYTHONPATH=.` starten |
| Ollama | Ein separat installierter Dienst, der sich selbst aktualisiert; dabei ist sein Port kurz weg |

## F. Häufige Fehler

| Symptom | Abhilfe |
|---------|---------|
| `ModuleNotFoundError: realtime_subtitle` | Aus dem Repo-Wurzelverzeichnis starten; neu pullen; das venv muss zu diesem Klon gehören |
| `ModuleNotFoundError: PyQt5` | Der Code ist neuer als das venv — das Update-Skript die Abhängigkeiten neu installieren lassen (v3.0.0 ist auf PyQt6 umgestiegen) |
| `import config` schlägt in alten Notizen fehl | `from realtime_subtitle import config` verwenden |
| Nur Ausgangssprache, keine Übersetzung | Ollama läuft nicht oder das Modell wurde nie geladen: `ollama list` mit `config.OLLAMA_MODEL` vergleichen |
| In der ersten Minute nach der Installation keine Übersetzung | Das Modell lädt noch, während die Spracherkennung den Rückstau abarbeitet; das gibt sich von selbst |
| Verknüpfung meldet „Datei nicht gefunden" | `scripts\windows\install.ps1` erneut ausführen, um die `.bat`-Dateien neu zu erzeugen |
| Installationspfad mit Nicht-ASCII-Zeichen | Klon nach z. B. `C:\realtime_subtitle` verschieben und neu installieren |
| Kein Ton wird erfasst | Aufgenommen wird das **Standard-Wiedergabegerät**; ein Headset-Wechsel greift nach ca. 5 s. Für ein festes Gerät im ⚙-Panel einen Namensteil eintragen |

---

Vertiefendes Material — Hardwarestufen, Tuning-Parameter und die vollständige Liste der
Fallen, in die dieses Projekt schon getappt ist — steht in
[CLAUDE.md](../../CLAUDE.md) (auf Chinesisch).
