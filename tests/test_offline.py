import json
from pathlib import Path
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
            return {"id": "demo", "title": "Demo", "extractor": "youtube"}

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(offline, "download_video", lambda url, job_dir, max_height: ({"id": "demo", "title": "Demo", "extractor": "youtube"}, video))
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
    assert (tmp_path / "out" / "youtube_demo" / "demo_bilingual.srt").read_text(encoding="utf-8-sig").endswith(
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
    assert len(seen) == 2, "已完成的第 1 条不该再作为当前条目请求"
    assert all("德语原文：\nEins\n" not in p for p in seen)
    assert "Eins" in seen[0]  # 只作为 Zwei 的上一句语境


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


def test_different_jobs_can_lock_in_parallel(tmp_path):
    with offline.JobLock(tmp_path / "a" / "_job.lock"):
        with offline.JobLock(tmp_path / "b" / "_job.lock"):
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

    job_dir = tmp_path / "out" / "youtube_demo"
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


def test_truncated_ollama_response_is_rejected():
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"response": "不完整的译文", "done": True, "done_reason": "length"},
    )
    session = SimpleNamespace(post=lambda *args, **kwargs: response)
    with pytest.raises(offline.TruncatedModelOutput):
        offline._ollama_request(session, "unused", "unused", "unused")


def test_ollama_request_rejects_empty_error_and_incomplete():
    def call(payload):
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
        session = SimpleNamespace(post=lambda *args, **kwargs: response)
        return offline._ollama_request(session, "u", "m", "p")

    with pytest.raises(offline.OfflineSubtitleError):
        call({"error": "model busy"})
    with pytest.raises(offline.OfflineSubtitleError):
        call({"response": "", "done": True, "done_reason": "stop"})
    with pytest.raises(offline.OfflineSubtitleError):
        call({"response": "还没写完", "done": False, "done_reason": "stop"})
    with pytest.raises(offline.OfflineSubtitleError):
        call({"response": 123, "done": True, "done_reason": "stop"})
    assert call({"response": "完整译文", "done": True, "done_reason": "stop"}) == "完整译文"


def test_truncated_translation_is_not_checkpointed(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    rows = [
        {"start": 0, "end": 1, "text": "Eins"},
        {"start": 1, "end": 2, "text": "Zwei"},
    ]

    def fake_req(session, url, model, prompt, num_predict=512):
        if "Zwei" in prompt:
            raise offline.TruncatedModelOutput("模型输出达到上限被截断。")
        return "译"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    ckpt = tmp_path / "_job_checkpoint.json"
    with pytest.raises(offline.OfflineSubtitleError):
        offline.translate_segments(rows, "de", "zh", checkpoint_path=ckpt)
    assert rows[0].get("translation") == "译"
    assert not rows[1].get("translation")
    loaded = offline.load_checkpoint(ckpt)
    assert loaded["rows"][1].get("translation") in (None, "")


def test_learning_guide_failure_keeps_existing_note_and_srt(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    dest = tmp_path / "guide.md"
    dest.write_text("# 旧笔记\n", encoding="utf-8")
    srt = tmp_path / "demo_bilingual.srt"
    srt.write_text("keep me", encoding="utf-8-sig")

    def boom(*args, **kwargs):
        raise offline.TruncatedModelOutput("截断")

    monkeypatch.setattr(offline, "_ollama_request", boom)
    rows = [{"start": 0, "end": 1, "text": "Hallo", "translation": "你好"}]
    with pytest.raises(offline.OfflineSubtitleError):
        offline.write_learning_guide(rows, "Titel", "https://example.test/x", 1.0, "de", "zh", dest)
    assert dest.read_text(encoding="utf-8") == "# 旧笔记\n"
    assert srt.read_text(encoding="utf-8-sig") == "keep me"


def test_typical_guide_fits_default_context(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    prompts = []

    def fake_req(session, url, model, prompt, num_predict=512):
        prompts.append((prompt, num_predict))
        if "必须输出 Markdown" in prompt:
            return (
                "## 内容概述\n一段概述\n"
                "## 对话脉络\n- 第一条\n"
                "## 重点词汇与表达\n- Wort — 词；提示\n"
                "## 学习方法\n1. 听原文\n"
            )
        return "摘要"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    monkeypatch.setattr(offline.config, "OLLAMA_NUM_CTX", 4096)
    rows = [
        {
            "start": i * 5,
            "end": i * 5 + 5,
            "text": "Das ist ein ganz normaler deutscher Satz zum Lernen.",
            "translation": "这是一个用于学习的普通德语句子。",
        }
        for i in range(30)
    ]
    dest = tmp_path / "guide.md"
    offline.write_learning_guide(rows, "Demo", "https://example.test/x", 150, "de", "zh", dest)
    assert dest.is_file()
    for prompt, num_predict in prompts:
        budget = offline.input_char_budget(output_tokens=num_predict)
        assert len(prompt) <= budget, f"{len(prompt)} > {budget} (num_predict={num_predict})"


@pytest.mark.parametrize("candidate_count", [0, 1, 24])
def test_guide_candidate_block_respects_budget(tmp_path, monkeypatch, candidate_count):
    _stub_ollama_local(monkeypatch)
    prompts = []

    def fake_req(session, url, model, prompt, num_predict=512):
        prompts.append(prompt)
        if "必须输出 Markdown" in prompt:
            return (
                "## 内容概述\n概述\n## 对话脉络\n- a\n"
                "## 重点词汇与表达\n- x\n## 学习方法\n1. 听\n"
            )
        return "摘要"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    monkeypatch.setattr(offline.config, "OLLAMA_NUM_CTX", 4096)
    rows = []
    for i in range(max(candidate_count, 1)):
        source = ("Wort " * 18).strip() if candidate_count else "Hi"
        rows.append({
            "start": float(i),
            "end": float(i + 1),
            "text": source if candidate_count else "Hi",
            "translation": "词" * 40,
        })
    if candidate_count == 0:
        rows = [{"start": 0, "end": 1, "text": "Hi", "translation": "嗨"}]
    dest = tmp_path / "guide.md"
    offline.write_learning_guide(rows, "T", "https://example.test/x", 10, "de", "zh", dest)
    final_budget = offline.input_char_budget(output_tokens=1800)
    finals = [p for p in prompts if "必须输出 Markdown" in p]
    assert finals
    assert all(len(p) <= final_budget for p in finals)
    if candidate_count == 0:
        assert "不要编造" in finals[-1] or "无合适候选" in finals[-1]
        assert "预算不足暂不展示" not in finals[-1]
    elif candidate_count == 1:
        assert "选 1 条" in finals[-1]


def test_long_video_keeps_vocab_candidates_after_summary_merge(tmp_path, monkeypatch):
    """☠️ 240 条合格字幕 / 20 分钟：先删光候选再归并摘要的话，腾出预算也不会恢复。

    复现条件与交接方案一致：每条都符合候选长度，模型摘要替身约 140 字。
    最终 prompt 必须仍含预算能装下的原始表达，不能写成「本次无合适候选」。
    """
    _stub_ollama_local(monkeypatch)
    finals = []

    def fake_req(session, url, model, prompt, num_predict=512):
        if "必须输出 Markdown" in prompt:
            finals.append(prompt)
            return (
                "## 内容概述\n一段概述\n"
                "## 对话脉络\n- 第一条\n"
                "## 重点词汇与表达\n- Wort — 词；提示\n"
                "## 学习方法\n1. 听原文\n"
            )
        return "这是一个用于确认长视频归并行为的摘要。" * 7

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    monkeypatch.setattr(offline.config, "OLLAMA_NUM_CTX", 4096)
    rows = [
        {
            "start": i * 5.0,
            "end": i * 5.0 + 5.0,
            "text": f"Wir lernen heute viele neue deutsche Ausdruecke Nummer {i:03d}.",
            "translation": "今天学习新的德语表达。",
        }
        for i in range(240)
    ]
    dest = tmp_path / "guide.md"
    offline.write_learning_guide(rows, "test", "https://example.test/x", 1200, "de", "zh", dest)
    assert finals, "没有发出最终学习笔记 prompt"
    prompt = finals[-1]
    budget = offline.input_char_budget(output_tokens=1800)
    assert len(prompt) <= budget
    assert "本次无合适候选" not in prompt
    assert "Ausdruecke" in prompt
    vocab_lines = [line for line in prompt.splitlines() if line.startswith("- ") and "Ausdruecke" in line]
    assert len(vocab_lines) >= 3, f"最终词汇输入太少：{len(vocab_lines)}"
    assert dest.is_file()


def test_guide_rejects_heading_only_sections():
    headings_only = "\n".join(offline._GUIDE_SECTIONS)
    assert offline._guide_has_required_sections(headings_only) is False

    missing_overview_body = (
        "## 内容概述\n"
        "## 对话脉络\n- 第一条\n"
        "## 重点词汇与表达\n- Wort — 词\n"
        "## 学习方法\n1. 听原文\n"
    )
    assert offline._guide_has_required_sections(missing_overview_body) is False

    missing_one_heading = (
        "## 内容概述\n一段概述\n"
        "## 对话脉络\n- 第一条\n"
        "## 学习方法\n1. 听原文\n"
    )
    assert offline._guide_has_required_sections(missing_one_heading) is False

    allowed_method_suffix = (
        "## 内容概述\n一段概述\n"
        "## 对话脉络\n- 第一条\n"
        "## 重点词汇与表达\n本次无合适候选，字幕里没有适合单独列出的表达。\n"
        "## 学习方法（配合双语SRT）\n1. 听原文\n"
    )
    assert offline._guide_has_required_sections(allowed_method_suffix) is True


def test_heading_only_guide_does_not_overwrite_old_note(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    dest = tmp_path / "guide.md"
    dest.write_text("# 旧笔记\n保留\n", encoding="utf-8")

    def fake_req(session, url, model, prompt, num_predict=512):
        if "必须输出 Markdown" in prompt:
            return "\n".join(offline._GUIDE_SECTIONS)
        return "摘要"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    rows = [{"start": 0, "end": 1, "text": "Hallo zusammen, wir sprechen heute.", "translation": "你好"}]
    with pytest.raises(offline.OfflineSubtitleError, match="不完整"):
        offline.write_learning_guide(rows, "T", "https://example.test/x", 10, "de", "zh", dest)
    assert dest.read_text(encoding="utf-8") == "# 旧笔记\n保留\n"


def test_translation_prompt_keeps_previous_sentence_readonly():
    prompt = offline._translation_prompt(
        "de", "zh", "nur, mutmaßlich, vermutlich, möglicherweise.",
        previous_text="Vier Wochen lang",
    )
    assert "Vier Wochen lang" in prompt
    assert "nur, mutmaßlich, vermutlich, möglicherweise." in prompt
    assert "不要把邻句译入" in prompt or "不要翻译进本条" in prompt or "只作语境" in prompt
    assert offline.TX_STRATEGY_VERSION >= 3


def test_guide_prompt_allows_fewer_outline_points_when_material_is_short():
    prompt = offline._final_guide_prompt("摘要", "- Wort → 词", 1)
    assert "素材不足" in prompt
    assert "视频中表示" in prompt or "主持人认为" in prompt


def test_input_budget_rejects_too_small_context():
    with pytest.raises(offline.OfflineSubtitleError):
        offline.input_char_budget(num_ctx=100, output_tokens=90)


def test_second_job_is_rejected_before_download(tmp_path, monkeypatch):
    events = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            return {"id": "demo", "title": "Demo", "extractor": "youtube"}

    class BlockingLock:
        def __init__(self, path):
            pass

        def __enter__(self):
            events.append("lock")
            raise offline.OfflineSubtitleError("already locked")

        def __exit__(self, *args):
            return False

    def download(*args, **kwargs):
        events.append("download")
        return {}, tmp_path / "demo.mp4"

    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(offline, "download_video", download)
    monkeypatch.setattr(offline, "JobLock", BlockingLock)
    with pytest.raises(offline.OfflineSubtitleError, match="already locked"):
        offline.process_url("https://example.test/demo", tmp_path / "out", summary=False)
    assert events == ["lock"]


def test_cross_extractor_does_not_reuse_cached_media(tmp_path, monkeypatch):
    job = tmp_path / "123"
    job.mkdir()
    (job / "123.mp4").write_bytes(b"site_a_media")
    downloaded = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if download:
                downloaded.append(True)
                dest = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                dest.write_bytes(b"site_b_media")
            return {"id": "123", "title": "B", "extractor": "site_b"}

    monkeypatch.setattr(offline.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    with pytest.raises(offline.OfflineSubtitleError, match="来源"):
        offline.download_video("https://site-b.test/123", job)
    assert downloaded == []
    assert (job / "123.mp4").read_bytes() == b"site_a_media"


def test_matching_source_reuses_media_and_legacy_cache_is_not_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(offline.shutil, "which", lambda name: "ffmpeg")

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if download:
                dest = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
                dest.write_bytes(b"fresh")
            return {"id": "abc", "title": "A", "extractor": "youtube"}

    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    job = tmp_path / "youtube_abc"
    job.mkdir()
    video = job / "youtube_abc.mp4"
    video.write_bytes(b"cached")
    offline.save_media_source(job, "youtube", "abc", "https://example.test/abc")
    info, found = offline.download_video("https://example.test/abc", job)
    assert info["extractor"] == "youtube"
    assert found.read_bytes() == b"cached"

    legacy = tmp_path / "abc"
    legacy.mkdir()
    (legacy / "abc.mp4").write_bytes(b"old")
    with pytest.raises(offline.OfflineSubtitleError, match="来源"):
        offline.download_video("https://example.test/abc", legacy)


def test_process_url_separates_same_id_from_different_extractors(tmp_path, monkeypatch):
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            extractor = "site_a" if "site-a" in url else "site_b"
            return {"id": "123", "title": extractor, "extractor": extractor}

    def fake_download(url, job_dir, max_height=2160):
        calls.append((url, job_dir.name))
        job_dir.mkdir(parents=True, exist_ok=True)
        video = job_dir / f"{job_dir.name}.mp4"
        video.write_bytes(url.encode("utf-8"))
        extractor = "site_a" if "site-a" in url else "site_b"
        return {"id": "123", "title": extractor, "extractor": extractor}, video

    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(offline, "download_video", fake_download)
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline,
        "transcribe_audio",
        lambda audio, source_language: ([{"start": 0, "end": 1, "text": "Hallo"}], "de", 1),
    )
    monkeypatch.setattr(
        offline,
        "translate_segments",
        lambda rows, source, target, **kw: rows[0].update(translation="你好"),
    )

    first = offline.process_url("https://site-a.test/123", tmp_path / "out", summary=False)
    second = offline.process_url("https://site-b.test/123", tmp_path / "out", summary=False)
    assert first["video"] != second["video"]
    assert {name for _, name in calls} == {"site_a_123", "site_b_123"}
    assert first["video"].read_bytes() == b"https://site-a.test/123"
    assert second["video"].read_bytes() == b"https://site-b.test/123"


def test_learning_guide_prompts_stay_within_budget(tmp_path, monkeypatch):
    _stub_ollama_local(monkeypatch)
    prompts = []

    def fake_req(session, url, model, prompt, num_predict=512):
        prompts.append((len(prompt), num_predict))
        if "必须输出 Markdown" in prompt:
            return (
                "## 内容概述\n概述\n## 对话脉络\n- a\n"
                "## 重点词汇与表达\n- x\n## 学习方法\n1. 听\n"
            )
        return "摘要"

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    rows = [
        {"start": float(i), "end": float(i + 5), "text": ("Langer Satz. " * 40), "translation": "长句"}
        for i in range(0, 400, 5)
    ]
    dest = tmp_path / "guide.md"
    offline.write_learning_guide(
        rows, "Titel", "https://example.test/x", 400.0, "de", "zh", dest)
    for length, npred in prompts:
        limit = offline.input_char_budget(output_tokens=npred)
        assert length <= limit, f"prompt {length} exceeded budget {limit} (num_predict={npred})"
    assert dest.is_file()
