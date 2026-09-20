"""批次 4：分享链接解析、平台失败提示、凭据来源、阶段耗时。

☠️ **本文件没有访问任何平台**，一次真实下载都没有。分享文案样本全部是自己
编的合成文本（不抄真实帖子内容）。因此这里能证明的只有：
  - 分享文案里的链接被完整、正确地抠出来（含中文标点边界）；
  - 平台报错被翻成用户能行动的中文，并指向本地导入；
  - 凭据只从显式配置来，且不出现在日志/任务元数据/断点里；
  - 阶段耗时被记录下来（真实数字要在真机长视频上跑）。
**不能据此声称任何小红书链接可以下载。**
"""
import time
from types import SimpleNamespace

import pytest

import realtime_subtitle.offline as offline
from realtime_subtitle import config


# ======================================================================
# 分享文案里的链接：中文标点不是空白字符
# ======================================================================


@pytest.mark.parametrize("text, expected", [
    # 短链后面直接跟中文逗号——`https?://\S+` 会把后半句一起吞掉
    ("复制此信息，打开【小红书】App查看精彩内容！ http://xhslink.com/a/AbC123，快去看",
     ["http://xhslink.com/a/AbC123"]),
    # 查询串必须完整保留：xsec_token 是分享链接的一部分，掉了就打不开
    ("看这个 https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
     "?xsec_token=ABC&xsec_source=pc_feed 记得点赞。",
     ["https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
      "?xsec_token=ABC&xsec_source=pc_feed"]),
    # 全角右括号收尾
    ("（详见 https://example.test/item/42?t=1）",
     ["https://example.test/item/42?t=1"]),
    # 中文句号 / 感叹号分隔的两个链接
    ("视频在这里：https://example.test/v1。另外还有 https://example.test/v2！",
     ["https://example.test/v1", "https://example.test/v2"]),
    # URL 自带成对括号，不能剥
    ("参考 https://en.wikipedia.org/wiki/Foo_(bar) 完",
     ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    # 半角句号结尾要剥
    ("见 https://example.test/x.", ["https://example.test/x"]),
    # 重复链接去重、保序
    ("https://example.test/a 和 https://example.test/a 还有 https://example.test/b",
     ["https://example.test/a", "https://example.test/b"]),
    ("没有链接的一段话", []),
])
def test_share_text_urls_stop_at_cjk_punctuation(text, expected):
    assert offline.extract_share_urls(text) == expected


def test_a_blurb_with_one_link_is_accepted_directly():
    blurb = "复制此信息打开App！ https://example.test/only 快看"

    assert offline.resolve_share_input(blurb) == "https://example.test/only"


def test_a_blurb_with_several_links_asks_instead_of_guessing(tmp_path, monkeypatch):
    """不替用户随机挑一个：分享文案里常同时有短链、活动页和下载页。"""
    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: pytest.fail("还没定用哪个链接就不该联网"))
    blurb = "活动 https://example.test/a 视频 https://example.test/b"

    with pytest.raises(offline.OfflineSubtitleError, match="2 个链接"):
        offline.process_url(blurb, tmp_path / "out")


def test_cli_can_list_urls_without_touching_the_network(capsys, monkeypatch):
    """PowerShell 靠这个模式复用同一份规则，不再各写一套正则。"""
    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: pytest.fail("--list-urls 不该联网"))

    assert offline.main([
        "打开App看！ http://xhslink.com/a/Zz9，还有 https://example.test/v2。",
        "--list-urls",
    ]) == 0

    assert capsys.readouterr().out.split() == [
        "http://xhslink.com/a/Zz9", "https://example.test/v2"]


def test_a_local_path_is_not_mistaken_for_share_text(tmp_path):
    clip = tmp_path / "我的 片段.mp4"
    clip.write_bytes(b"media")

    assert offline.resolve_share_input(str(clip)) == str(clip)


# ======================================================================
# 平台失败要能看懂，并且指向本地导入
# ======================================================================


@pytest.mark.parametrize("raw, expect", [
    ("ERROR: Sign in to confirm your age", "需要登录"),
    ("Use --cookies-from-browser or --cookies for the authentication", "需要登录"),
    ("ERROR: Private video. Sign in if you've been granted access", "需要登录"),
    ("This video is only available to members-only content", "会员"),
    ("ERROR: The uploader has not made this video available in your country", "地区"),
    ("HTTP Error 429: Too Many Requests", "限流"),
    ("ERROR: Unsupported URL: https://example.test/x", "认不出"),
    ("ERROR: Video unavailable. This video has been removed", "失效"),
])
def test_platform_failures_are_translated_for_humans(raw, expect):
    err = offline._friendly_download_error(
        RuntimeError(raw), "https://example.test/x", "视频下载失败")

    text = str(err)
    assert expect in text
    assert "本地导入" in text or "拖进输入框" in text, "每一类都要给出路"
    assert raw in text, "原始报错仍要保留，方便贴 issue"


def test_unknown_failures_still_point_at_local_import():
    err = offline._friendly_download_error(
        RuntimeError("something entirely new"), "https://example.test/x", "视频下载失败")

    assert "本地导入" in str(err) or "拖进输入框" in str(err)


def test_extraction_failure_surfaces_the_friendly_message(tmp_path, monkeypatch):
    class FailingYDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("ERROR: Sign in to confirm you're not a bot")

    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: SimpleNamespace(YoutubeDL=FailingYDL))
    monkeypatch.setattr(offline, "transcribe_audio",
                        lambda *a, **kw: pytest.fail("导入失败不该走到 ASR"))

    with pytest.raises(offline.OfflineSubtitleError, match="需要登录"):
        offline.process_url("https://example.test/x", tmp_path / "out")


# ======================================================================
# 凭据：只从显式配置来，且不许泄漏
# ======================================================================


def test_no_credentials_by_default(monkeypatch):
    monkeypatch.delattr(config, "OFFLINE_COOKIES_FILE", raising=False)
    monkeypatch.delattr(config, "OFFLINE_COOKIES_FROM_BROWSER", raising=False)

    assert offline.cookie_options() == {}
    assert "cookiefile" not in offline._ydl_options()
    assert "cookiesfrombrowser" not in offline._ydl_options()


def test_cookie_file_must_be_configured_explicitly(tmp_path, monkeypatch):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# netscape", encoding="utf-8")
    monkeypatch.setattr(config, "OFFLINE_COOKIES_FILE", str(jar), raising=False)
    monkeypatch.delattr(config, "OFFLINE_COOKIES_FROM_BROWSER", raising=False)

    assert offline._ydl_options()["cookiefile"] == str(jar)


def test_cookies_from_browser_accepts_a_name_or_a_profile(monkeypatch):
    monkeypatch.delattr(config, "OFFLINE_COOKIES_FILE", raising=False)
    monkeypatch.setattr(config, "OFFLINE_COOKIES_FROM_BROWSER", "chrome", raising=False)
    assert offline._ydl_options()["cookiesfrombrowser"] == ("chrome",)

    monkeypatch.setattr(config, "OFFLINE_COOKIES_FROM_BROWSER",
                        ["chrome", "Default"], raising=False)
    assert offline._ydl_options()["cookiesfrombrowser"] == ("chrome", "Default")


def test_missing_cookie_file_fails_without_printing_the_path(tmp_path, monkeypatch):
    secret = tmp_path / "私密" / "cookies.txt"
    monkeypatch.setattr(config, "OFFLINE_COOKIES_FILE", str(secret), raising=False)

    with pytest.raises(offline.OfflineSubtitleError) as caught:
        offline.cookie_options()

    assert str(secret) not in str(caught.value), "报错里不许出现凭据路径"
    assert "OFFLINE_COOKIES_FILE" in str(caught.value)


def test_credentials_never_reach_job_metadata_or_checkpoint(tmp_path, monkeypatch):
    """任务元数据和断点是用户会直接贴进 issue 的文件。"""
    jar = tmp_path / "cookies.txt"
    jar.write_text("# netscape", encoding="utf-8")
    monkeypatch.setattr(config, "OFFLINE_COOKIES_FILE", str(jar), raising=False)
    monkeypatch.setattr(config, "OFFLINE_COOKIES_FROM_BROWSER", "chrome", raising=False)
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline, "transcribe_audio",
        lambda audio, source_language: (
            [{"start": 0.0, "end": 1.0, "text": "Hallo"}], "de", 1.0))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        offline, "translate_segments",
        lambda rows, s, t, **kw: [r.update(translation="你好") for r in rows])
    monkeypatch.setattr(offline, "_load_yt_dlp",
                        lambda: pytest.fail("本地任务不联网"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"media-bytes")

    result = offline.process_local_file(clip, tmp_path / "out", summary=False)

    job = tmp_path / "out" / result["media"].job_key
    for name in (offline.MEDIA_SOURCE_NAME, "_job_checkpoint.json"):
        blob = (job / name).read_text(encoding="utf-8")
        assert "cookie" not in blob.lower(), name
        assert str(jar) not in blob, name
        assert "chrome" not in blob.lower(), name


# ======================================================================
# 阶段耗时：长视频要能回答"时间花在哪"
# ======================================================================


def test_stage_timer_reports_each_stage_and_the_realtime_ratio():
    timer = offline.StageTimer()
    with timer.measure("识别"):
        time.sleep(0.01)
    timer.skip("抽音频")

    text = timer.summary(media_seconds=120.0)

    assert "识别" in text and "抽音频" in text
    assert "复用缓存" in text, "跳过的阶段要标出来，别把'没跑'读成'很快'"
    assert "实时倍率" in text
    assert timer.stages["抽音频"] == 0.0


def test_process_media_records_stage_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline, "transcribe_audio",
        lambda audio, source_language: (
            [{"start": 0.0, "end": 60.0, "text": "Hallo"}], "de", 60.0))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        offline, "translate_segments",
        lambda rows, s, t, **kw: [r.update(translation="你好") for r in rows])
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: pytest.fail("本地任务不联网"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"media-bytes")
    out = tmp_path / "out"

    first = offline.process_local_file(clip, out, summary=False)

    assert set(first["stage_seconds"]) >= {"抽音频", "识别", "翻译"}
    assert first["stages_reused"] == []
    assert first["duration"] == 60.0

    second = offline.process_local_file(clip, out, summary=False)

    assert set(second["stages_reused"]) == {"抽音频", "识别"}, "复用要如实标出来"
    assert second["stage_seconds"]["识别"] == 0.0


def test_stage_summary_is_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(offline, "_extract_audio", lambda video, audio: None)
    monkeypatch.setattr(
        offline, "transcribe_audio",
        lambda audio, source_language: (
            [{"start": 0.0, "end": 30.0, "text": "Hallo"}], "de", 30.0))
    monkeypatch.setattr(offline, "_has_audio_stream", lambda path: True)
    monkeypatch.setattr(
        offline, "translate_segments",
        lambda rows, s, t, **kw: [r.update(translation="你好") for r in rows])
    monkeypatch.setattr(offline, "_load_yt_dlp", lambda: pytest.fail("本地任务不联网"))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"media-bytes")

    offline.process_local_file(clip, tmp_path / "out", summary=False)

    assert "阶段耗时" in capsys.readouterr().out


# ======================================================================
# yt-dlp 对小红书的实际支持范围（读本机装的那个版本，不联网）
# ======================================================================


def test_recorded_xiaohongshu_support_matches_the_installed_yt_dlp():
    """把"装的这版 yt-dlp 认哪些小红书链接"钉下来。

    ☠️ 这条只说明**正则认不认**，不代表能下载成功——那要真实链接和网络，
    见 CLAUDE.md 第 39 条里的待验项。短链 xhslink.com 不在 _VALID_URL 里，
    要靠 generic extractor 跟跳转，本地无法证明。
    """
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE

    assert XiaoHongShuIE.suitable(
        "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9")
    assert XiaoHongShuIE.suitable(
        "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15"
        "?xsec_token=ABC")
    assert not XiaoHongShuIE.suitable("http://xhslink.com/a/AbC123"), (
        "短链不在 _VALID_URL 里；这是已知限制，不是回归")
    _ = yt_dlp
