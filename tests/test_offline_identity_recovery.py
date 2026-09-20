"""批次 2/3 补修：媒体身份、断点恢复、阶段依赖、音轨证据、任务锁身份。

对应审核 R01–R06。这里的用例大多**不用整段替身**：
- R02 走真实的 translate_segments / save_checkpoint / load_checkpoint /
  process_local_file，只把 Ollama 的那一次 HTTP 换掉；整段 fake 掉
  translate_segments 会正好把持久化 bug 盖住。
- R05 用真实 ffmpeg 造带/不带音轨的短文件，真实跑 ffprobe（本机可执行，
  不需要任何平台授权）。

仍然没有：真实下载、真实 Whisper/GPU、真实平台样本。
"""
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import realtime_subtitle.offline as offline


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------


def _wav(path: Path, seconds: float = 18.2, sample_rate: int = 44100) -> Path:
    """写一个真正的 PCM WAV（不是"扩展名是 .wav 的随机字节"）。"""
    frames = int(seconds * sample_rate)
    body = b"".join(struct.pack("<h", (i * 37) % 3000 - 1500) for i in range(frames))
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(body)
    return path


def _flip_byte(path: Path, offset: int) -> None:
    """改中间某个字节，**不改文件大小**——这正是采样指纹漏掉的那种变化。"""
    with open(path, "r+b") as fh:
        fh.seek(offset)
        original = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([original[0] ^ 0xFF]))


def _stub_asr(monkeypatch, rows=None, detected="de"):
    calls = []

    def fake_transcribe(audio, source_language):
        calls.append(source_language)
        body = rows if rows is not None else [
            {"start": 0.0, "end": 1.0, "text": "Eins"},
        ]
        return ([dict(r) for r in body], detected, float(len(body)))

    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(offline, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: pytest.fail("本地任务不许碰下载器"))
    return calls


def _stub_translate(monkeypatch):
    calls = []

    def fake_translate(rows, source, target, **kw):
        todo = [r for r in rows if not (r.get("translation") or "").strip()]
        if todo:
            calls.append((source, target, len(todo)))
        for row in todo:
            row["translation"] = "<%s>%s" % (target, row["text"])

    monkeypatch.setattr(offline, "translate_segments", fake_translate)
    return calls


# ======================================================================
# R01：本地身份必须覆盖**全部**内容
# ======================================================================


@pytest.mark.parametrize("size, offset", [
    (1_500_000, 1_200_000),   # 1～2MiB：v1 完全不读尾部
    (3_000_000, 1_500_000),   # >2MiB：v1 不读中间
])
def test_fingerprint_notices_changes_anywhere_in_the_file(tmp_path, size, offset):
    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes((i * 7) % 251 for i in range(size)))
    before = offline.local_media_fingerprint(path)

    _flip_byte(path, offset)

    assert path.stat().st_size == size, "大小没变，变的只有内容"
    assert offline.local_media_fingerprint(path) != before


def test_fingerprint_notices_changes_in_a_real_wav(tmp_path):
    """审核用 1,600,044 字节的有效 WAV 复现过，这里照做。"""
    path = _wav(tmp_path / "clip.wav", seconds=18.2)
    size = path.stat().st_size
    assert size > 1_500_000, size
    before = offline.local_media_fingerprint(path)

    _flip_byte(path, 1_200_000)

    assert path.stat().st_size == size
    assert offline.local_media_fingerprint(path) != before


def test_fingerprint_carries_an_algorithm_version(tmp_path):
    """带版本前缀，v1 的采样指纹永远不会被当成完整内容身份继承。"""
    path = _wav(tmp_path / "clip.wav", seconds=0.2)

    assert offline.local_media_fingerprint(path).startswith(
        f"v{offline.LOCAL_FINGERPRINT_VERSION}_")


def test_changed_content_does_not_reuse_the_old_subtitles(tmp_path, monkeypatch):
    asr = _stub_asr(monkeypatch)
    _stub_translate(monkeypatch)
    path = _wav(tmp_path / "clip.wav", seconds=18.2)
    out = tmp_path / "out"

    first = offline.process_local_file(path, out, summary=False)
    _flip_byte(path, 1_200_000)
    second = offline.process_local_file(path, out, summary=False)

    assert len(asr) == 2, "内容变了必须重新识别，不能复用旧字幕"
    assert first["media"].content_id != second["media"].content_id


# ======================================================================
# R03：改名/移动同一份内容不该重跑 ASR
# ======================================================================


def test_renaming_a_local_file_reuses_asr_and_translations(tmp_path, monkeypatch):
    asr = _stub_asr(monkeypatch)
    translated = _stub_translate(monkeypatch)
    path = _wav(tmp_path / "a.wav", seconds=0.5)
    out = tmp_path / "out"

    offline.process_local_file(path, out, summary=False)
    renamed = path.rename(tmp_path / "b.wav")
    again = offline.process_local_file(renamed, out, summary=False)

    assert len(asr) == 1, "改个名不是另一个视频，不该重跑 ASR"
    assert len(translated) == 1, "也不该丢掉已经翻好的译文"
    assert again["bilingual_srt"].is_file()


def test_same_content_at_a_different_path_reuses_everything(tmp_path, monkeypatch):
    asr = _stub_asr(monkeypatch)
    translated = _stub_translate(monkeypatch)
    original = _wav(tmp_path / "a.wav", seconds=0.5)
    elsewhere = tmp_path / "sub"
    elsewhere.mkdir()
    copy = elsewhere / "same.wav"
    shutil.copyfile(original, copy)
    out = tmp_path / "out"

    offline.process_local_file(original, out, summary=False)
    offline.process_local_file(copy, out, summary=False)

    assert len(asr) == 1
    assert len(translated) == 1


def test_origin_is_updated_to_the_new_path(tmp_path, monkeypatch):
    """origin 只是"从哪来的"，会跟着更新，但不参与身份判定。"""
    _stub_asr(monkeypatch)
    _stub_translate(monkeypatch)
    path = _wav(tmp_path / "a.wav", seconds=0.5)
    out = tmp_path / "out"

    first = offline.process_local_file(path, out, summary=False)
    renamed = path.rename(tmp_path / "b.wav")
    second = offline.process_local_file(renamed, out, summary=False)

    assert first["media"].origin.endswith("a.wav")
    assert second["media"].origin.endswith("b.wav")
    job = out / second["media"].job_key
    saved = json.loads((job / offline.MEDIA_SOURCE_NAME).read_text(encoding="utf-8"))
    assert saved["source_url"].endswith("b.wav")


# ======================================================================
# R02：翻译中断恢复不许重翻已完成的部分
#
# ☠️ 这一组**不 fake translate_segments**：真实翻译循环 + 真实 checkpoint 存取，
# 只把 Ollama 那一次请求换掉。整段 fake 会正好盖住持久化不同步这个 bug。
# ======================================================================


def _stub_ollama(monkeypatch, responder):
    import realtime_subtitle.translate.translator_queue as tq
    monkeypatch.setattr(tq, "_assert_local_ollama", lambda url: True)
    monkeypatch.setattr(tq, "ollama_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(offline.time, "sleep", lambda s: None)
    seen = []

    def fake_req(session, url, model, prompt, num_predict=512):
        seen.append(prompt)
        return responder(prompt)

    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    return seen


def _two_row_job(monkeypatch, tmp_path):
    asr = _stub_asr(monkeypatch, rows=[
        {"start": 0.0, "end": 1.0, "text": "Eins"},
        {"start": 1.0, "end": 2.0, "text": "Zwei"},
    ])
    return asr, _wav(tmp_path / "clip.wav", seconds=0.5), tmp_path / "out"


def _current_segment(prompt, text):
    """prompt 里上一句也会出现，所以要判"当前这一条"而不是"出现过"。"""
    return prompt.rstrip().endswith(text) or f"原文：\n{text}\n" in prompt


def test_interrupted_translation_keeps_finished_rows_on_disk(tmp_path, monkeypatch):
    asr, video, out = _two_row_job(monkeypatch, tmp_path)

    def responder(prompt):
        if "Zwei" in prompt and not _current_segment(prompt, "Eins"):
            raise offline.OfflineSubtitleError("服务暂时不可用")
        return "一"

    _stub_ollama(monkeypatch, responder)
    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_local_file(video, out, summary=False)

    media = offline.resolve_media(video, out)
    saved = json.loads(
        (out / media.job_key / "_job_checkpoint.json").read_text(encoding="utf-8"))
    key = offline.translation_key("de", "zh")
    assert saved["rows"][0]["translation"] == "一"
    assert saved["translations"][key]["texts"][0] == "一", (
        "rows 和当前语言的 texts 必须一起落盘，否则重跑会把它清掉重翻")
    assert len(asr) == 1


def test_resume_only_translates_the_missing_row(tmp_path, monkeypatch):
    asr, video, out = _two_row_job(monkeypatch, tmp_path)
    broken = {"on": True}

    def responder(prompt):
        if "Zwei" in prompt and not _current_segment(prompt, "Eins"):
            if broken["on"]:
                raise offline.OfflineSubtitleError("服务暂时不可用")
            return "二"
        return "一"

    _stub_ollama(monkeypatch, responder)
    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_local_file(video, out, summary=False)

    broken["on"] = False
    seen = _stub_ollama(monkeypatch, responder)
    result = offline.process_local_file(video, out, summary=False)

    assert len(asr) == 1, "恢复不该重跑 ASR"
    assert len(seen) == 1, f"只该请求没翻完的那一条，实际 {len(seen)} 次"
    assert "Zwei" in seen[0]
    body = result["bilingual_srt"].read_text(encoding="utf-8-sig")
    assert "Eins\n一" in body and "Zwei\n二" in body


def test_interrupted_second_target_does_not_disturb_the_first(tmp_path, monkeypatch):
    asr, video, out = _two_row_job(monkeypatch, tmp_path)
    _stub_ollama(monkeypatch, lambda prompt: "zh")
    offline.process_local_file(video, out, summary=False)  # de→zh 全部完成

    broken = {"on": True}

    def responder(prompt):
        if "Zwei" in prompt and not _current_segment(prompt, "Eins"):
            if broken["on"]:
                raise offline.OfflineSubtitleError("服务暂时不可用")
            return "two"
        return "one"

    _stub_ollama(monkeypatch, responder)
    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_local_file(video, out, summary=False, target_language="en")

    broken["on"] = False
    seen = _stub_ollama(monkeypatch, responder)
    offline.process_local_file(video, out, summary=False, target_language="en")
    assert len(seen) == 1, "英文只剩第二条没翻"

    seen = _stub_ollama(monkeypatch, lambda prompt: pytest.fail("旧目标不该重翻"))
    back = offline.process_local_file(video, out, summary=False, target_language="zh")

    assert seen == []
    assert len(asr) == 1
    assert "Eins\nzh" in back["bilingual_srt"].read_text(encoding="utf-8-sig")


def test_needs_review_flag_survives_a_restart(tmp_path, monkeypatch):
    asr, video, out = _two_row_job(monkeypatch, tmp_path)

    def responder(prompt):
        if "Zwei" in prompt and not _current_segment(prompt, "Eins"):
            return "正常译文"
        return "抱歉，我无法完成这个翻译请求。"

    _stub_ollama(monkeypatch, responder)
    offline.process_local_file(video, out, summary=False)

    media = offline.resolve_media(video, out)
    saved = json.loads(
        (out / media.job_key / "_job_checkpoint.json").read_text(encoding="utf-8"))
    key = offline.translation_key("de", "zh")

    assert saved["rows"][0]["needs_review"] is True
    assert saved["translations"][key]["review"][0] is True
    assert saved["translations"][key]["review"][1] is False


# ======================================================================
# R04：source 模式不该被翻译服务阻断
# ======================================================================


def test_source_only_never_calls_the_translator(tmp_path, monkeypatch):
    _stub_asr(monkeypatch)
    monkeypatch.setattr(
        offline, "translate_segments",
        lambda *a, **kw: pytest.fail("只要原文时不该调用翻译"))
    video = _wav(tmp_path / "clip.wav", seconds=0.5)

    result = offline.process_local_file(video, tmp_path / "out", summary=False,
                                        subtitle_mode="source")

    assert result["source_srt"].is_file()
    assert "Eins" in result["source_srt"].read_text(encoding="utf-8-sig")
    assert result["subtitle"] == result["source_srt"]


def test_source_only_reports_only_what_it_produced(tmp_path, monkeypatch):
    _stub_asr(monkeypatch)
    monkeypatch.setattr(offline, "translate_segments",
                        lambda *a, **kw: pytest.fail("不该调用翻译"))
    video = _wav(tmp_path / "clip.wav", seconds=0.5)

    result = offline.process_local_file(video, tmp_path / "out", summary=False,
                                        subtitle_mode="source")

    assert result["target_srt"] is None
    assert result["bilingual_srt"] is None
    assert result["guide"] is None
    job = result["source_srt"].parent
    assert not list(job.glob("*.target.srt"))
    assert not list(job.glob("*.bilingual.srt"))


def test_source_with_summary_survives_a_dead_translator(tmp_path, monkeypatch):
    """笔记要原文+译文成对，所以会尝试翻译；翻译挂了只跳过笔记，不撤销字幕。"""
    _stub_asr(monkeypatch)

    def boom(*a, **kw):
        raise offline.OfflineSubtitleError("服务不可用")

    monkeypatch.setattr(offline, "translate_segments", boom)
    video = _wav(tmp_path / "clip.wav", seconds=0.5)

    result = offline.process_local_file(video, tmp_path / "out", summary=True,
                                        subtitle_mode="source")

    assert result["source_srt"].is_file()
    assert result["guide"] is None


def test_other_modes_still_require_the_translation(tmp_path, monkeypatch):
    """别把 source 的放行扩大到 target/bilingual：那两种确实需要译文。"""
    _stub_asr(monkeypatch)

    def boom(*a, **kw):
        raise offline.OfflineSubtitleError("服务不可用")

    monkeypatch.setattr(offline, "translate_segments", boom)
    video = _wav(tmp_path / "clip.wav", seconds=0.5)

    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_local_file(video, tmp_path / "out", summary=False,
                                   subtitle_mode="target")


# ======================================================================
# R05：acodec 未知 ≠ 确定没有音轨
# ======================================================================


@pytest.mark.parametrize("formats, rejected, why", [
    ([{"acodec": "none"}], True, "全部明确无音频"),
    ([{"acodec": "none"}, {"acodec": "none"}], True, "全部明确无音频"),
    ([{}], False, "缺字段=未知"),
    ([{"acodec": None}], False, "None=未知"),
    ([{"acodec": ""}], False, "空串=未知"),
    ([{"acodec": "mp4a.40.2"}], False, "明确有音频"),
    ([{"acodec": "none"}, {}], False, "混合：有未知就不能提前判死"),
    ([{"acodec": "none"}, {"acodec": "opus"}], False, "混合：有一路有音频"),
])
def test_only_definite_evidence_rejects_a_link(formats, rejected, why):
    info = {"id": "x", "extractor": "demo", "title": "t", "formats": formats}

    if rejected:
        with pytest.raises(offline.OfflineSubtitleError, match="音轨"):
            offline._assert_single_playable(info, "https://example.test/x")
    else:
        offline._assert_single_playable(info, "https://example.test/x")  # 不抛


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="需要本机 ffmpeg/ffprobe")
def test_real_ffprobe_tells_audio_from_silence_free_video(tmp_path):
    """真实 ffmpeg 造两个短文件，真实 ffprobe 判定——本机可执行，不需要平台授权。"""
    with_audio = tmp_path / "with_audio.mp4"
    without_audio = tmp_path / "no_audio.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-shortest", str(with_audio)],
        check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-an", str(without_audio)],
        check=True)

    assert offline._has_audio_stream(with_audio) is True
    assert offline._has_audio_stream(without_audio) is False


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="需要本机 ffmpeg/ffprobe")
def test_real_soundless_local_file_is_rejected_before_asr(tmp_path, monkeypatch):
    silent = tmp_path / "no_audio.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-an", str(silent)],
        check=True)
    calls = []
    monkeypatch.setattr(offline, "transcribe_audio",
                        lambda *a, **kw: calls.append(1) or ([], "de", 0.0))

    with pytest.raises(offline.OfflineSubtitleError, match="音轨"):
        offline.process_local_file(silent, tmp_path / "out", summary=False)

    assert calls == []


# ======================================================================
# R06：下载回来的身份必须和上锁时的身份一致
# ======================================================================


def _ydl(monkeypatch, info):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return info

    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))


def test_identity_change_during_download_stops_cleanly(tmp_path, monkeypatch):
    """锁的是 a 的目录，下载却说自己是 b —— 必须停，且不能是裸 FileNotFoundError。"""
    calls = []
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(offline, "transcribe_audio",
                        lambda *a, **kw: calls.append(1) or ([], "de", 0.0))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    _ydl(monkeypatch, {"id": "a", "extractor": "site_a", "title": "A"})
    video = tmp_path / "b.mp4"
    video.write_bytes(b"media")
    monkeypatch.setattr(
        offline, "download_video",
        lambda url, job_dir, max_height, info=None: (
            {"id": "b", "extractor": "site_b", "title": "B"}, video))
    out = tmp_path / "out"

    with pytest.raises(offline.OfflineSubtitleError, match="身份"):
        offline.process_url("https://example.test/a", out)

    assert calls == [], "身份不一致时不能走到 ASR"
    assert not (out / "site_b_b").exists(), "更不能往没上锁的目录里写"


def test_matching_identity_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline, "transcribe_audio",
        lambda audio, source_language: (
            [{"start": 0.0, "end": 1.0, "text": "Hallo"}], "de", 1.0))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    _stub_translate(monkeypatch)
    _ydl(monkeypatch, {"id": "a", "extractor": "site_a", "title": "A"})
    video = tmp_path / "a.mp4"
    video.write_bytes(b"media")
    monkeypatch.setattr(
        offline, "download_video",
        lambda url, job_dir, max_height, info=None: (
            {"id": "a", "extractor": "site_a", "title": "A"}, video))

    result = offline.process_url("https://example.test/a", tmp_path / "out",
                                 summary=False)

    assert result["bilingual_srt"].parent.name == "site_a_a"


def test_two_tasks_on_the_same_url_are_still_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    _ydl(monkeypatch, {"id": "a", "extractor": "site_a", "title": "A"})
    out = tmp_path / "out"
    job_dir = out / "site_a_a"

    with offline.JobLock(job_dir / "_job.lock"):
        with pytest.raises(offline.OfflineSubtitleError, match="已在运行"):
            offline.process_url("https://example.test/a", out, summary=False)
