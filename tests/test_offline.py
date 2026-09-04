from types import SimpleNamespace

import realtime_subtitle.offline as offline
from realtime_subtitle.offline import build_srt, target_language_for


def test_target_language_policy():
    assert target_language_for("zh") == "de"
    assert target_language_for("zh-CN") == "de"
    assert target_language_for("de") == "zh"
    assert target_language_for("en") == "zh"
    assert target_language_for("fr") == "zh"
    assert target_language_for("und") == "zh"


def test_build_srt_puts_source_before_translation():
    rows = [
        {"start": 0, "end": 1.25, "text": "Guten Morgen", "translation": "早上好"},
        {"start": 2, "end": 3, "text": "Bis später", "translation": "回头见"},
    ]

    bilingual = build_srt(rows)
    source_only = build_srt(rows, bilingual=False)

    assert "00:00:00,000 --> 00:00:01,250\nGuten Morgen\n早上好" in bilingual
    assert "Bis später\n回头见" in bilingual
    assert "Guten Morgen\n早上好" not in source_only
    assert "Guten Morgen" in source_only


def test_process_url_wires_source_and_bilingual_outputs(tmp_path, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "demo", "title": "Demo"}

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(offline, "download_video", lambda url, job_dir, max_height: ({"id": "demo", "title": "Demo"}, video))
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline,
        "transcribe_audio",
        lambda audio, source_language: ([{"start": 0, "end": 1, "text": "Guten Morgen"}], "de", 1),
    )
    monkeypatch.setattr(offline, "translate_segments", lambda rows, source, target: rows[0].update(translation="早上好"))

    result = offline.process_url("https://example.test/demo", tmp_path / "out", summary=False)

    assert result["source_language"] == "de"
    assert result["target_language"] == "zh"
    assert result["guide"] is None
    assert (tmp_path / "out" / "demo" / "demo_bilingual.srt").read_text(encoding="utf-8-sig").endswith(
        "Guten Morgen\n早上好\n"
    )
