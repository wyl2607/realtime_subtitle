import json
from types import SimpleNamespace

import pytest

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
    monkeypatch.setattr(
        offline,
        "translate_segments",
        lambda rows, source, target, **kw: rows[0].update(translation="早上好"),
    )

    result = offline.process_url("https://example.test/demo", tmp_path / "out", summary=False)

    assert result["source_language"] == "de"
    assert result["target_language"] == "zh"
    assert result["guide"] is None
    assert (tmp_path / "out" / "demo" / "demo_bilingual.srt").read_text(encoding="utf-8-sig").endswith(
        "Guten Morgen\n早上好\n"
    )


def _stub_ollama_local(monkeypatch):
    import realtime_subtitle.translate.translator_queue as tq
    monkeypatch.setattr(tq, "_assert_local_ollama", lambda url: True)
    monkeypatch.setattr(tq, "ollama_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(offline.time, "sleep", lambda s: None)


def test_translate_resumes_completed_rows(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    rows = [
        {"start": 0, "end": 1, "text": "Eins"},
        {"start": 1, "end": 2, "text": "Zwei"},
        {"start": 2, "end": 3, "text": "Drei"},
    ]
    seen = []
    fail_zwei = {"on": True}

    def fake_req(session, url, model, prompt, num_predict=512):
        seen.append(prompt)
        if "Zwei" in prompt and fail_zwei["on"]:
            raise RuntimeError("boom")
        return "译"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    ckpt = tmp_path / "_job_checkpoint.json"
    with pytest.raises(offline.OfflineSubtitleError):
        offline.translate_segments(rows, "de", "zh", checkpoint_path=ckpt)
    assert rows[0].get("translation") == "译"
    assert not rows[1].get("translation")
    fail_zwei["on"] = False
    seen.clear()
    offline.translate_segments(rows, "de", "zh", checkpoint_path=ckpt)
    assert all(r.get("translation") == "译" for r in rows)
    assert not any("Eins" in p for p in seen), "已完成的第 1 条不该再请求"


def test_checkpoint_rejects_language_change(tmp_path):
    path = tmp_path / "ckpt.json"
    rows = [{"start": 0, "end": 1, "text": "Hallo", "translation": "你好"}]
    data = offline.build_checkpoint(
        video_id="demo", source_url="https://example.test/demo",
        extractor="youtube", source_language="de", target_language="zh",
        rows=rows, asr_done=True, complete=False,
    )
    offline.save_checkpoint(path, data)
    loaded = offline.load_checkpoint(path)
    assert loaded["rows"][0]["translation"] == "你好"
    assert offline.checkpoint_asr_usable(loaded, "demo", "de") is True
    assert offline.checkpoint_asr_usable(loaded, "demo", "zh") is False
    assert offline.checkpoint_tx_usable(loaded, "de", "zh") is True
    assert offline.checkpoint_tx_usable(loaded, "de", "en") is False
    assert offline.checkpoint_asr_usable(
        loaded, "demo", "de", "https://example.test/other", "youtube",
    ) is False


def test_checkpoint_fingerprints_include_runtime_settings(monkeypatch):
    from realtime_subtitle import config

    asr = offline.asr_fingerprint("de")
    monkeypatch.setattr(config, "WHISPER_COMPUTE_TYPE", "changed")
    assert offline.asr_fingerprint("de") != asr

    tx = offline.tx_fingerprint("de", "zh")
    monkeypatch.setattr(config, "GLOSSARY", {"new": "新"})
    assert offline.tx_fingerprint("de", "zh") != tx


def test_corrupt_checkpoint_is_ignored(tmp_path):
    path = tmp_path / "ckpt.json"
    path.write_text("{ 坏", encoding="utf-8")
    assert offline.load_checkpoint(path) is None


def test_semantically_corrupt_checkpoint_is_ignored(tmp_path):
    path = tmp_path / "ckpt.json"
    path.write_text(
        '{"version": 1, "rows": [{"start": 0, "text": null}]}',
        encoding="utf-8",
    )
    assert offline.load_checkpoint(path) is None


def test_atomic_srt_replace(tmp_path):
    dest = tmp_path / "out.srt"
    offline.atomic_write_text(dest, "hello", encoding="utf-8-sig")
    assert dest.read_text(encoding="utf-8-sig") == "hello"
    assert not list(tmp_path.glob("*.tmp"))


def test_job_lock_blocks_second_holder(tmp_path):
    lock = tmp_path / "_job.lock"
    with offline.JobLock(lock):
        with pytest.raises(offline.OfflineSubtitleError):
            with offline.JobLock(lock):
                pass


def test_process_url_skips_asr_when_checkpoint_matches(tmp_path, monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "demo", "title": "Demo", "extractor": "youtube"}

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(offline, "download_video", lambda url, job_dir, max_height: ({"id": "demo", "title": "Demo", "extractor": "youtube"}, video))
    extracts = []
    transcribes = []
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: extracts.append(1))
    monkeypatch.setattr(
        offline,
        "transcribe_audio",
        lambda audio, source_language: transcribes.append(1) or (
            [{"start": 0, "end": 1, "text": "Guten Morgen"}], "de", 1),
    )
    monkeypatch.setattr(offline, "translate_segments", lambda *a, **k: a[0][0].update(translation="早上好") if a else None)

    job_dir = tmp_path / "out" / "demo"
    job_dir.mkdir(parents=True)
    rows = [{"start": 0, "end": 1, "text": "Guten Morgen", "translation": "早上好"}]
    offline.save_checkpoint(job_dir / "_job_checkpoint.json", offline.build_checkpoint(
        video_id="demo", source_url="https://example.test/demo",
        extractor="youtube", source_language="de", target_language="zh",
        rows=rows, asr_done=True, complete=False,
    ))
    offline.process_url("https://example.test/demo", tmp_path / "out", source_language="de", summary=False)
    assert extracts == []
    assert transcribes == []


def test_chunk_rows_by_char_budget_keeps_all_rows():
    rows = [{"start": i, "end": i + 1, "text": "Wort " * 20, "translation": "词"} for i in range(6)]
    chunks = offline.chunk_rows_for_summary(rows, budget=400)
    assert sum(len(c) for c in chunks) == 6
    assert all(len(offline._summary_chunk_prompt(c)) <= 400 for c in chunks)
    assert chunks[0][0]["start"] == 0
    assert chunks[-1][-1]["end"] == 6


def test_chunk_rows_rejects_single_oversized_row():
    rows = [{"start": 0, "end": 1, "text": "Wort " * 200, "translation": "词"}]
    with pytest.raises(offline.OfflineSubtitleError):
        offline.chunk_rows_for_summary(rows, budget=400)


def test_merge_summaries_keeps_head_and_tail():
    summaries = [
        {"text": "开头内容", "start": 0, "end": 10},
        {"text": "中间" * 40, "start": 10, "end": 20},
        {"text": "结尾内容", "start": 20, "end": 30},
    ]
    merged = offline.merge_summaries_to_budget(summaries, budget=80)
    assert merged[0]["start"] == 0
    assert merged[-1]["end"] == 30
    blob = "".join(s["text"] for s in merged)
    assert "开头" in blob and "结尾" in blob


def test_input_budget_rejects_too_small_context():
    with pytest.raises(offline.OfflineSubtitleError):
        offline.input_char_budget(num_ctx=100, output_tokens=90)


def test_learning_guide_prompts_stay_within_budget(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    prompts = []

    def fake_req(session, url, model, prompt, num_predict=512):
        prompts.append((len(prompt), num_predict))
        return "摘要"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    rows = [
        {"start": float(i), "end": float(i + 5), "text": ("Langer Satz. " * 40), "translation": "长句"}
        for i in range(0, 400, 5)
    ]
    dest = tmp_path / "guide.md"
    offline.write_learning_guide(
        rows, "Titel", "https://example.test/x", 400.0, "de", "zh", dest)
    chunk_budget = offline.input_char_budget(output_tokens=320)
    final_budget = offline.input_char_budget(output_tokens=1800)
    for length, npred in prompts:
        limit = chunk_budget if npred <= 400 else final_budget
        assert length <= limit + 200, f"prompt {length} exceeded budget {limit} (num_predict={npred})"
    assert dest.is_file()
