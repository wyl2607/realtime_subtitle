"""批次 3：导入层与处理层的边界（本地文件、URL 导入校验、任务互斥）。

☠️ 全部替身，**没有真实下载、没有真实模型**。这些用例证明的是
"导入层做了哪些校验"和"处理层确实碰不到网站"，不能据此声称任何平台已验收。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import realtime_subtitle.offline as offline


def _no_network(monkeypatch):
    """处理层但凡碰一下 yt-dlp 就炸——这是这一批要守的边界。"""
    def boom(*args, **kwargs):
        raise AssertionError("处理层不许访问网站")

    monkeypatch.setattr(offline, "_load_yt_dlp", boom)
    monkeypatch.setattr(offline, "download_video", boom)


def _stub_processing(monkeypatch, detected="de", text="Hallo"):
    calls = {"asr": [], "translate": []}
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)

    def fake_transcribe(audio, source_language):
        calls["asr"].append((Path(audio).name, source_language))
        return ([{"start": 0.0, "end": 1.0, "text": text}], detected, 1.0)

    def fake_translate(rows, source, target, **kw):
        todo = [r for r in rows if not (r.get("translation") or "").strip()]
        if todo:
            calls["translate"].append((source, target))
        for row in todo:
            row["translation"] = "<%s>%s" % (target, row["text"])

    monkeypatch.setattr(offline, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(offline, "translate_segments", fake_translate)
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    return calls


def _local_video(tmp_path, name="clip.mp4", body=b"local-media-bytes"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


# ======================================================================
# 本地文件导入：走同一条处理链，但一次都不碰下载器
# ======================================================================


def test_local_file_runs_the_full_pipeline_without_any_downloader(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    calls = _stub_processing(monkeypatch)
    video = _local_video(tmp_path)

    result = offline.process_local_file(video, tmp_path / "out", summary=False)

    assert calls["asr"] and calls["translate"] == [("de", "zh")]
    assert result["bilingual_srt"].is_file()
    assert result["source_language"] == "de"
    assert result["media"].is_local is True
    assert result["media"].extractor == "local"


def test_local_media_is_identified_by_content_not_by_name(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    _stub_processing(monkeypatch)
    first = _local_video(tmp_path, "a.mp4", b"same-bytes")
    renamed = _local_video(tmp_path, "b.mp4", b"same-bytes")
    edited = _local_video(tmp_path, "c.mp4", b"different-bytes")
    out = tmp_path / "out"

    a = offline.process_local_file(first, out, summary=False)
    b = offline.process_local_file(renamed, out, summary=False)
    c = offline.process_local_file(edited, out, summary=False)

    assert a["media"].content_id == b["media"].content_id, "改个名不是另一个视频"
    assert a["media"].content_id != c["media"].content_id, "改了内容就是另一个"
    assert a["bilingual_srt"].parent == b["bilingual_srt"].parent
    assert a["bilingual_srt"].parent != c["bilingual_srt"].parent


def test_local_media_is_not_disguised_as_a_url(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    _stub_processing(monkeypatch)
    video = _local_video(tmp_path)

    media = offline.resolve_media(video, tmp_path / "out")

    assert media.origin == str(video.resolve())
    assert not offline.looks_like_url(media.origin)
    assert media.job_key.startswith("local_")


def test_local_file_is_never_copied_or_renamed(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    _stub_processing(monkeypatch)
    video = _local_video(tmp_path)
    before = video.read_bytes()

    result = offline.process_local_file(video, tmp_path / "out", summary=False)

    assert video.is_file() and video.read_bytes() == before
    assert result["video"] == video.resolve()


def test_local_asr_is_reused_across_target_switches(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    calls = _stub_processing(monkeypatch, detected="zh", text="你好")
    video = _local_video(tmp_path)
    out = tmp_path / "out"

    english = offline.process_local_file(video, out, summary=False)
    german = offline.process_local_file(video, out, summary=False,
                                        target_language="de")

    assert len(calls["asr"]) == 1, "换目标语言不该对本地文件重跑 ASR"
    assert calls["translate"] == [("zh", "en"), ("zh", "de")]
    assert english["bilingual_srt"].is_file() and german["bilingual_srt"].is_file()


@pytest.mark.parametrize("name, body, why", [
    ("notes.txt", b"text", "不支持的类型"),
    ("empty.mp4", b"", "空文件"),
])
def test_bad_local_files_are_rejected_before_asr(tmp_path, monkeypatch, name, body, why):
    _no_network(monkeypatch)
    calls = _stub_processing(monkeypatch)
    path = tmp_path / name
    path.write_bytes(body)

    with pytest.raises(offline.OfflineSubtitleError):
        offline.process_local_file(path, tmp_path / "out", summary=False)

    assert calls["asr"] == [], why


def test_missing_local_file_is_rejected(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    _stub_processing(monkeypatch)

    with pytest.raises(offline.OfflineSubtitleError, match="找不到"):
        offline.process_local_file(tmp_path / "nope.mp4", tmp_path / "out")


def test_local_file_without_audio_track_is_rejected(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    calls = _stub_processing(monkeypatch)
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: False)
    video = _local_video(tmp_path)

    with pytest.raises(offline.OfflineSubtitleError, match="音轨"):
        offline.process_local_file(video, tmp_path / "out", summary=False)

    assert calls["asr"] == [], "导入失败绝不能走到 ASR"


def test_process_local_file_refuses_a_url(tmp_path):
    with pytest.raises(offline.OfflineSubtitleError, match="网络地址"):
        offline.process_local_file("https://example.test/demo", tmp_path)


# ======================================================================
# URL 导入的边界：合集 / 无音轨 / 失败不启动 ASR
# ======================================================================


def _fake_ydl(monkeypatch, info, downloads=None):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if downloads is not None:
                downloads.append(download)
            return info

    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: SimpleNamespace(YoutubeDL=FakeYoutubeDL))


def test_playlist_is_rejected_with_a_readable_message(tmp_path, monkeypatch):
    calls = _stub_processing(monkeypatch)
    _fake_ydl(monkeypatch, {"_type": "playlist", "id": "list1",
                            "entries": [{"id": "a"}, {"id": "b"}]})
    monkeypatch.setattr(offline, "download_video",
                        lambda *a, **kw: pytest.fail("合集不该开始下载"))

    with pytest.raises(offline.OfflineSubtitleError, match="合集"):
        offline.process_url("https://example.test/list", tmp_path / "out")

    assert calls["asr"] == []


def test_photo_post_without_audio_is_rejected(tmp_path, monkeypatch):
    calls = _stub_processing(monkeypatch)
    _fake_ydl(monkeypatch, {"id": "pic1", "extractor": "demo", "title": "图文",
                            "formats": [{"acodec": "none", "vcodec": "none"}]})
    monkeypatch.setattr(offline, "download_video",
                        lambda *a, **kw: pytest.fail("没有音轨就不该下载"))

    with pytest.raises(offline.OfflineSubtitleError, match="音轨"):
        offline.process_url("https://example.test/pic", tmp_path / "out")

    assert calls["asr"] == []


def test_downloaded_media_without_audio_is_rejected_before_asr(tmp_path, monkeypatch):
    calls = _stub_processing(monkeypatch)
    _fake_ydl(monkeypatch, {"id": "demo", "extractor": "demo", "title": "Demo"})
    video = _local_video(tmp_path, "demo.mp4")
    monkeypatch.setattr(offline, "download_video",
                        lambda url, job_dir, max_height, info=None: (
                            {"id": "demo", "extractor": "demo", "title": "Demo"}, video))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: False)

    with pytest.raises(offline.OfflineSubtitleError, match="音轨"):
        offline.process_url("https://example.test/demo", tmp_path / "out")

    assert calls["asr"] == []


def test_url_info_is_parsed_once_not_twice(tmp_path, monkeypatch):
    """同一次任务不重复解析：任务目录的身份要来自同一份解析结果。"""
    _stub_processing(monkeypatch)
    probes = []
    _fake_ydl(monkeypatch, {"id": "demo", "extractor": "demo", "title": "Demo"},
              downloads=probes)
    video = _local_video(tmp_path, "demo.mp4")
    seen_info = []

    def fake_download(url, job_dir, max_height, info=None):
        seen_info.append(info)
        return {"id": "demo", "extractor": "demo", "title": "Demo"}, video

    monkeypatch.setattr(offline, "download_video", fake_download)

    offline.process_url("https://example.test/demo", tmp_path / "out", summary=False)

    assert probes == [False], "只该有一次只读解析"
    assert seen_info and seen_info[0] is not None, "解析结果要传给下载器复用"


# ======================================================================
# 处理层与导入层的职责边界
# ======================================================================


def test_process_media_accepts_a_prepared_media_and_skips_import(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    calls = _stub_processing(monkeypatch)
    video = _local_video(tmp_path)
    media = offline.MediaSource(
        path=video, extractor="local", content_id="deadbeef",
        title="手工构造", origin=str(video), is_local=True)
    options = offline.build_task_options(summary=False)

    result = offline.process_media(media, options, tmp_path / "out")

    assert calls["asr"]
    assert result["bilingual_srt"].parent.name == "local_deadbeef"


def test_task_options_are_frozen():
    options = offline.build_task_options(target_language="de")

    with pytest.raises(Exception):
        options.target_language = "en"


def test_build_task_options_validates_before_anything_else():
    with pytest.raises(offline.OfflineSubtitleError):
        offline.build_task_options(target_language="英语")
    with pytest.raises(offline.OfflineSubtitleError):
        offline.build_task_options(subtitle_mode="monolingual")
    with pytest.raises(offline.OfflineSubtitleError):
        offline.build_task_options(max_height=0)


# ======================================================================
# 两个同视频任务互斥（下载在锁**之内**）
# ======================================================================


def test_second_task_on_the_same_local_file_is_rejected(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    _stub_processing(monkeypatch)
    video = _local_video(tmp_path)
    out = tmp_path / "out"
    media = offline.resolve_media(video, out)
    job_lock = out / media.job_key / "_job.lock"

    with offline.JobLock(job_lock):
        with pytest.raises(offline.OfflineSubtitleError, match="已在运行"):
            offline.process_local_file(video, out, summary=False)


def test_download_happens_inside_the_job_lock(tmp_path, monkeypatch):
    """下载必须在锁内：两个进程同时下同一个视频会互相写坏任务目录。"""
    _stub_processing(monkeypatch)
    _fake_ydl(monkeypatch, {"id": "demo", "extractor": "demo", "title": "Demo"})
    video = _local_video(tmp_path, "demo.mp4")
    events = []

    class RecordingLock(offline.JobLock):
        def __enter__(self):
            events.append("lock")
            return super().__enter__()

    def fake_download(url, job_dir, max_height, info=None):
        events.append("download")
        return {"id": "demo", "extractor": "demo", "title": "Demo"}, video

    monkeypatch.setattr(offline, "JobLock", RecordingLock)
    monkeypatch.setattr(offline, "download_video", fake_download)

    offline.process_url("https://example.test/demo", tmp_path / "out", summary=False)

    assert events[:2] == ["lock", "download"], events
    assert events.count("lock") == 1, "整个任务只该上一次锁，不能中途放开"


def test_ps1_accepts_a_local_file(tmp_path):
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "windows" / "download_subtitle.ps1").read_text(
        encoding="utf-8-sig")

    assert "本地文件" in text
    assert "Test-Path -LiteralPath $Url -PathType Leaf" in text
    # 拖放进来的路径自带引号，不去掉会被当成网络地址
    assert ".Trim('\"')" in text
