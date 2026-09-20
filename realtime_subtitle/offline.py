"""Download media, transcribe it locally, and export bilingual subtitles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from realtime_subtitle import config
from realtime_subtitle.paths import repo_path


_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
# 本地导入也接受纯音频：管线从 _extract_audio 之后就只认音频了
_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".opus", ".ogg"}
_LOCAL_MEDIA_EXTENSIONS = _VIDEO_EXTENSIONS | _AUDIO_EXTENSIONS

# ☠️ **译文内容永远不是控制信号，它只能产出"建议人工复核"的提示。**
#
# 这里踩过两级坑。第一级是子串黑名单 ("无法完成", "法律法规", "政治敏感", ...)：
# 一句完全合法的译文「我们必须遵守法律法规。」被判成拒答 → 重试 → 换模型 →
# 仍然"失败"，整段视频停在那一条上，而模型一直在正常干活。
# 第二级是"改成锚开头的整句模式 + 长度上限就能区分了"——**也不行**。
# 「抱歉，我不能帮你。」既可能是模型在拒答，也可能就是影片里角色说的话；
# 「Sorry, I cannot help you.」同理。原文是 "Es tut mir leid, ich kann dir
# nicht helfen."，正确译文和拒答的字面**可以完全相同**，改成 fullmatch 也
# 消除不了这个歧义。日常对白里这种句子很常见，拿它中断整片是主要功能错误。
#
# 所以现在这个函数只被用来打一个 needs_review 标记 + 打一行提示，**不参与
# 任何失败判定、也不触发重试或换模型**。真正的失败判据全部是结构化的：
# 空响应、服务 error、done=False、输出被 length 截断（_ollama_request 里）。
# 别再往这里加更大的黑名单、也别引入第二个模型来"判断是不是拒答"——
# 代价（整片失败）和收益（少一条怪译文）完全不对等。
_REFUSAL_MAX_CHARS = 120
_REFUSAL_PATTERNS = (
    re.compile(r"^(?:抱歉|很抱歉|对不起|不好意思)[，,。.！!]?\s*"
               r"(?:作为[^，,。]{0,16}[，,])?\s*我(?:无法|不能|没法|没有办法)"),
    re.compile(r"^我(?:无法|不能|没法|没有办法)(?:帮你|为你|替你)?"
               r"(?:翻译|完成|提供|处理|回答|继续)"),
    re.compile(r"^(?:作为一个?|作为)[^，,。]{0,16}[，,]\s*"
               r"我(?:无法|不能|没法|没有办法)"),
    re.compile(r"^(?:I'?m sorry|I am sorry|Sorry)[,，]?\s*(?:but\s+)?"
               r"I\s+(?:can'?t|cannot|am unable to|won'?t)\b", re.IGNORECASE),
    re.compile(r"^(?:As an AI|I)\b[^.]{0,48}\b(?:cannot|can'?t|won'?t)\s+"
               r"(?:assist|help|comply|translate|provide)\b", re.IGNORECASE),
)


def _looks_like_refusal(text: str) -> bool:
    """这段译文**看起来**像模型拒答吗——只用于复核提示，不用于判定失败。

    命中不代表它真是拒答（合法对白可以一模一样），不命中也不代表模型没拒答。
    调用方只能拿它打标记，见上面的注释。
    """
    body = (text or "").strip()
    if not body or len(body) > _REFUSAL_MAX_CHARS:
        return False
    return any(pattern.search(body) for pattern in _REFUSAL_PATTERNS)


class OfflineSubtitleError(RuntimeError):
    """A user-actionable failure in the batch download/subtitle pipeline."""


class TruncatedModelOutput(OfflineSubtitleError):
    """The model hit its output limit before finishing."""


@dataclass(frozen=True)
class MediaSource:
    """一份**已经在本地**的媒体，以及它是从哪来的。

    这是导入层和处理层之间唯一的交接物。处理层（process_media）拿到它之后
    不再关心平台：ASR / 翻译 / 导出里**不许出现"如果是小红书"这种分支**。
    本地文件用内容指纹标识，不伪装成网站链接（extractor='local'）。
    """
    path: Path
    extractor: str
    content_id: str
    title: str
    origin: str          # 网络任务是 URL，本地任务是文件绝对路径
    is_local: bool = False
    info: dict = field(default_factory=dict, repr=False)  # 平台原始元信息

    @property
    def job_key(self) -> str:
        return job_key(self.extractor, self.content_id)


@dataclass(frozen=True)
class TaskOptions:
    """一次任务的选项快照。

    ☠️ 进任务时拍一次快照就不再变：这些值会进缓存身份和导出名，中途被谁改一下
    就会出现"指纹按 A 算、文件按 B 写"的错位。所以是 frozen 的。
    """
    source_language: str = "auto"
    target_language: str | None = None
    subtitle_mode: str = "bilingual"
    summary: bool = True
    max_height: int = 2160


def build_task_options(
    source_language: str = "auto",
    target_language: str | None = None,
    subtitle_mode: str | None = None,
    summary: bool = True,
    max_height: int = 2160,
) -> TaskOptions:
    """校验并冻结任务选项。**在任何网络访问之前调用**，非法参数别等下载完才报。"""
    source_language = (source_language or "auto").strip().lower()
    if source_language != "auto":
        source_language = _validate_language_code(source_language, "源语言")
    explicit_target = None
    if target_language:
        explicit_target = _validate_language_code(target_language, "目标语言")
    if int(max_height) < 1:
        raise OfflineSubtitleError("最高画质高度必须是正整数。")
    return TaskOptions(
        source_language=source_language,
        target_language=explicit_target,
        subtitle_mode=normalize_subtitle_mode(subtitle_mode),
        summary=bool(summary),
        max_height=int(max_height),
    )


# 字幕模式：只影响渲染，**不进入任何缓存身份**（双语切单语不许重跑模型）。
SUBTITLE_MODES = ("bilingual", "target", "source")
DEFAULT_SUBTITLE_MODE = "bilingual"


def default_target_for(source_language: str) -> str:
    """没有显式选目标语言时，这个源语言默认翻成什么。

    ☠️ 这是**离线管线**的默认值，和实时字幕的 LANGUAGE_PAIRS 是两回事：
    中文视频离线默认出英文（2026-09-10 需求），而实时的中→德是德语学习用途，
    不能为了离线的一个默认值去改所有实时行为。非中文源仍然默认中文。
    显式选了 target 的一律以显式为准，检测结果不得覆盖它。
    """
    return "en" if (source_language or "").lower().startswith("zh") else "zh"


# 旧名字，保留给既有调用方/测试
target_language_for = default_target_for


def normalize_subtitle_mode(mode: str | None, bilingual: bool | None = None) -> str:
    """把 mode/bilingual 两种写法归一成一个模式码。"""
    if mode:
        mode = str(mode).strip().lower()
        if mode not in SUBTITLE_MODES:
            raise OfflineSubtitleError(
                f"未知字幕模式：{mode}（可选 {'/'.join(SUBTITLE_MODES)}）")
        return mode
    if bilingual is None:
        return DEFAULT_SUBTITLE_MODE
    return "bilingual" if bilingual else "source"


def _language_name(language: str) -> str:
    return getattr(config, "LANGUAGE_NAMES", {}).get(language, language)


def _target_language_name(language: str) -> str:
    return getattr(config, "TRANSLATION_TARGET_NAMES", {}).get(language, _language_name(language))


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis = 0
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(rows: list[dict], bilingual: bool = True, mode: str | None = None) -> str:
    """渲染 SRT。mode 优先于 bilingual（后者是旧签名，保留给既有调用方）。

    - bilingual：原文在上、译文在下（默认，延续原有导出体验）
    - target：**只有译文**（交互里叫"单语"——用户要的是英语/德语那一行，
      不是中文原文；以前 bilingual=False 出的是原文，直接拿它当"单语"会
      出中文）
    - source：只有原文，等价于旧的 bilingual=False，留给原文导出

    ☠️ 模式只影响这里的渲染，不进缓存身份：双语切单语不许触发重新翻译。
    target 模式下某行缺译文时退回原文，不能产出空字幕块。
    """
    mode = normalize_subtitle_mode(mode, bilingual)
    blocks = []
    for index, row in enumerate(rows, 1):
        source_text = row["text"].strip()
        target = row.get("translation", "").strip()
        if mode == "source":
            body = source_text
        elif mode == "target":
            body = target or source_text
        else:
            body = source_text
            if target:
                body += "\n" + target
        blocks.append(
            f"{index}\n{_srt_time(row['start'])} --> {_srt_time(row['end'])}\n{body}\n"
        )
    return "\n".join(blocks)


def _find_video(job_dir: Path, video_id: str) -> Path | None:
    candidates = [
        path for path in job_dir.glob(f"{video_id}.*")
        if path.suffix.lower() in _VIDEO_EXTENSIONS and not path.name.endswith(".part")
    ]
    if not candidates:
        return None
    return next((path for path in candidates if path.suffix.lower() == ".mp4"), max(candidates, key=lambda p: p.stat().st_size))


def cookie_options() -> dict:
    """凭据只从**显式配置**来，绝不自动去翻浏览器。

    config_local.py 里写其中之一（两个都不写就是不带任何凭据）：
        OFFLINE_COOKIES_FILE = r"D:\\私密\\cookies.txt"
        OFFLINE_COOKIES_FROM_BROWSER = "chrome"   # 或 ("chrome", "Default")

    ☠️ 返回值只交给 yt-dlp，**不许进日志、不许进 _media_source.json、
    不许进 checkpoint**——那些文件用户会直接贴到 issue 里。要排查就说
    "已启用 cookies"，不要打印来源路径或浏览器 profile。
    """
    options = {}
    cookie_file = getattr(config, "OFFLINE_COOKIES_FILE", None)
    if cookie_file:
        path = Path(str(cookie_file)).expanduser()
        if not path.is_file():
            raise OfflineSubtitleError(
                "配置的 cookies 文件不存在。请检查 config_local.py 里的 "
                "OFFLINE_COOKIES_FILE（这里不打印路径，避免泄露）。")
        options["cookiefile"] = str(path)
    browser = getattr(config, "OFFLINE_COOKIES_FROM_BROWSER", None)
    if browser:
        options["cookiesfrombrowser"] = (
            tuple(browser) if isinstance(browser, (list, tuple)) else (str(browser),))
    return options


def _ydl_options(**extra) -> dict:
    options = {"quiet": True, "noplaylist": True}
    options.update(cookie_options())
    options.update(extra)
    return options


# yt-dlp 的报错是给开发者看的英文，用户看到只会以为"程序坏了"。这里把常见的
# 几类翻成"你能做什么"，并且**每一类都指向本地导入**——本地文件不需要登录，
# 是权限/风控问题唯一确定可用的出路。
_DOWNLOAD_HINTS = (
    (("sign in", "log in", "login", "account", "cookies", "authenticat"),
     "这个链接需要登录后才能访问。"),
    (("private", "members-only", "member only", "paid", "vip"),
     "这是私密/会员内容，当前身份看不到。"),
    (("in your country", "geo", "region", "blocked in"),
     "这个内容在当前地区不可用。"),
    (("429", "rate limit", "too many requests", "risk", "verify"),
     "对方在限流或要求人机验证，稍后再试。"),
    (("unsupported url", "no video formats", "unable to extract"),
     "yt-dlp 认不出这个链接的视频（可能是平台改版，或这不是视频页）。"),
    (("404", "not found", "removed", "deleted", "unavailable"),
     "链接已失效或内容已被删除。"),
)
_LOCAL_IMPORT_HINT = (
    "可以先用别的方式把视频存到本地，再把文件拖进输入框——本地导入不联网、"
    "不需要登录。若确实需要凭据，在 config_local.py 里显式配置 "
    "OFFLINE_COOKIES_FILE 或 OFFLINE_COOKIES_FROM_BROWSER。")


def _friendly_download_error(exc: Exception, url: str, what: str) -> OfflineSubtitleError:
    detail = str(exc)
    lowered = detail.lower()
    for markers, message in _DOWNLOAD_HINTS:
        if any(marker in lowered for marker in markers):
            return OfflineSubtitleError(f"{message}{_LOCAL_IMPORT_HINT}\n原始报错：{detail}")
    return OfflineSubtitleError(f"{what}：{detail}\n{_LOCAL_IMPORT_HINT}")


def _load_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise OfflineSubtitleError(
            "缺少 yt-dlp。请运行 venv\\Scripts\\pip install -r requirements.txt 后重试。"
        ) from exc
    return yt_dlp


MEDIA_SOURCE_NAME = "_media_source.json"
MEDIA_SOURCE_VERSION = 1


def _video_id(info: dict, url: str) -> str:
    raw = str(info.get("id") or "").strip()
    if not raw:
        raw = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def _normalize_extractor(info: dict) -> str:
    raw = str(info.get("extractor_key") or info.get("extractor") or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw).strip("._") or "unknown"
    return cleaned.lower()


def job_key(extractor: str, video_id: str) -> str:
    extractor = (extractor or "unknown").strip() or "unknown"
    video_id = (video_id or "unknown").strip() or "unknown"
    name = f"{extractor}_{video_id}"
    if len(name) <= 120:
        return name
    digest = hashlib.sha256(f"{extractor}\0{video_id}".encode("utf-8")).hexdigest()[:16]
    return f"{extractor[:32]}_{digest}"


def save_media_source(job_dir: Path, extractor: str, video_id: str, url: str) -> None:
    payload = {
        "version": MEDIA_SOURCE_VERSION,
        "extractor": extractor,
        "video_id": video_id,
        "source_url": url,
    }
    atomic_write_text(
        Path(job_dir) / MEDIA_SOURCE_NAME,
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def load_media_source(job_dir: Path) -> dict | None:
    path = Path(job_dir) / MEDIA_SOURCE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MEDIA_SOURCE_VERSION:
        return None
    if not data.get("extractor") or not data.get("video_id"):
        return None
    return data


def media_source_matches(job_dir: Path, extractor: str, video_id: str) -> bool:
    data = load_media_source(job_dir)
    if not data:
        return False
    return data.get("extractor") == extractor and data.get("video_id") == video_id


def download_video(url: str, job_dir: Path, max_height: int = 2160,
                   info: dict | None = None) -> tuple[dict, Path]:
    """Download the best available video up to max_height plus audio.

    info：上游已经解析过就传进来，省掉一次重复的网络解析（也保证任务目录的
    身份和这里用的 id 出自同一份解析结果）。
    """
    if max_height < 1:
        raise OfflineSubtitleError("最高画质高度必须是正整数。")
    if not shutil.which("ffmpeg"):
        raise OfflineSubtitleError("找不到 ffmpeg。请安装 ffmpeg 并加入 PATH。")
    yt_dlp = _load_yt_dlp()
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    if info is None:
        try:
            with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise _friendly_download_error(exc, url, "无法读取视频信息") from exc
    extractor = _normalize_extractor(info)
    video_id = _video_id(info, url)
    existing = _find_video(job_dir, job_dir.name) or _find_video(job_dir, video_id)
    if existing:
        if media_source_matches(job_dir, extractor, video_id):
            return info, existing
        raise OfflineSubtitleError(
            "任务目录里已有无法确认来源的媒体文件，未复用缓存。请检查后重试或换输出目录。"
        )

    output = str(job_dir / f"{job_dir.name}.%(ext)s")
    options = _ydl_options(
        format=f"bv*[height<={max_height}]+ba/b",
        outtmpl=output,
        merge_output_format="mp4",
        retries=3,
        fragment_retries=3,
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise _friendly_download_error(exc, url, "视频下载失败") from exc

    extractor = _normalize_extractor(info)
    video_id = _video_id(info, url)
    video = _find_video(job_dir, job_dir.name)
    if not video:
        video = _find_video(job_dir, video_id)
    if not video:
        raise OfflineSubtitleError("视频下载完成，但没有找到合并后的视频文件。请检查 yt-dlp/ffmpeg。")
    save_media_source(job_dir, extractor, video_id, url)
    return info, video


def looks_like_url(source: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", str(source).strip()))


# 从分享文案里抠链接。**这是唯一一份规则**，PowerShell 那边通过
# `download_subtitle.py --list-urls` 调过来，不再各写一套正则（B03 的教训）。
#
# ☠️ 不能用 `https?://\S+`：中文标点**不是空白字符**。小红书那种
# 「复制此信息，打开【小红书】App…… http://xhslink.com/a/AbC123，快去看」
# 会被抠成 `http://xhslink.com/a/AbC123，快去看`——整句后半段跟着进了 URL，
# 下载器当然找不到。所以按"URL 里合法的字符"正向匹配，撞到 CJK 或全角标点
# 就停。查询串里的 `xsec_token=...&xsec_source=...` 必须完整保留：那是分享
# 链接的一部分，去掉就打不开了。
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_SHARE_URL_RE = re.compile(r"https?://" + _URL_CHARS + r"+", re.IGNORECASE)
# 句末标点会粘在链接尾巴上；括号成对时不能剥（有的 URL 自带括号）
_URL_TRAILING = ".,;:!?'\"<>"


def _trim_url_tail(url: str) -> str:
    url = url.rstrip(_URL_TRAILING)
    while url and url[-1] in ")]}":
        opener = {")": "(", "]": "[", "}": "{"}[url[-1]]
        if url.count(opener) >= url.count(url[-1]):
            break          # 成对，属于 URL 自己的
        url = url[:-1]
        url = url.rstrip(_URL_TRAILING)
    return url


def extract_share_urls(text: str) -> list[str]:
    """从一段分享文案里抠出所有链接（去重、保序）。"""
    found = []
    for match in _SHARE_URL_RE.finditer(str(text or "")):
        url = _trim_url_tail(match.group())
        if url and url not in found:
            found.append(url)
    return found


def resolve_share_input(source: str | Path) -> str | Path:
    """把用户给的东西归一成"一个可处理的输入"。

    ☠️ 多个链接时**要求选定，不替用户随机挑一个**：分享文案里常常同时有短链、
    活动页和下载页，挑错了就是下了个不相干的视频。
    """
    if isinstance(source, Path) or looks_like_url(source):
        return source
    text = str(source).strip().strip('"')
    if not text or Path(text).expanduser().is_file():
        return text
    urls = extract_share_urls(text)
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        listed = "\n".join(f"  {i}. {u}" for i, u in enumerate(urls, 1))
        raise OfflineSubtitleError(
            f"这段文字里有 {len(urls)} 个链接，请只给其中一个：\n{listed}")
    return text


# 身份算法版本。指纹带前缀，v1 那批采样指纹就永远不会被当成"完整内容身份"
# 复用（它们对应的旧任务目录留在原地，不擅自清理）。
LOCAL_FINGERPRINT_VERSION = 2
_FINGERPRINT_CHUNK = 1 << 20


def local_media_fingerprint(path: Path) -> str:
    """本地文件的身份 = **整个文件内容**的 SHA-256，不是文件名、也不是采样。

    ☠️ v1 只读首尾各 1MB：1–2MiB 的文件后半段根本没参与哈希，更大的文件中间
    整段都没参与。保持大小不变、改盲区里的字节，指纹一模一样——于是内容变了
    却复用上一份 ASR 结果，用户拿到的是旧字幕。这不是哈希被绕过，是那些字节
    压根没读。所以现在流式读全文件。

    代价是真的要把文件读一遍（磁盘带宽决定，大文件是秒级不是毫秒级），
    超过 1 秒会打一行提示。**别为了省这点时间改回采样**：算错身份的代价是
    静默给错字幕。也别拿 mtime 顶替内容校验——复制/解压都会改 mtime，
    而剪辑软件覆盖写可能不改。
    """
    path = Path(path)
    size = path.stat().st_size
    started = time.time()
    digest = hashlib.sha256()
    digest.update(f"{LOCAL_FINGERPRINT_VERSION}:{size}:".encode("ascii"))
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_FINGERPRINT_CHUNK)
            if not block:
                break
            digest.update(block)
    elapsed = time.time() - started
    if elapsed > 1.0:
        print(f"校验本地文件指纹：{size / (1 << 20):.0f} MiB，用时 {elapsed:.1f} 秒",
              flush=True)
    return f"v{LOCAL_FINGERPRINT_VERSION}_{digest.hexdigest()[:32]}"


def _has_audio_stream(path: Path) -> bool | None:
    """有可用音轨吗。返回 None = 没有 ffprobe，查不了（不拿它当失败）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return bool((done.stdout or "").strip())


def _format_audio_evidence(fmt) -> str:
    """这一路 format 对"有没有音频"给出了什么证据：yes / no / unknown。"""
    if not isinstance(fmt, dict) or "acodec" not in fmt:
        return "unknown"          # 平台没写这个字段
    acodec = fmt.get("acodec")
    if acodec is None:
        return "unknown"          # 写了但没值
    text = str(acodec).strip().lower()
    if not text or text in ("unknown", "null", "?"):
        return "unknown"
    return "no" if text == "none" else "yes"


def _assert_single_playable(info: dict, origin: str) -> None:
    """导入阶段就把"这不是一个可处理的单个视频"挡掉，别让它走到 ASR。"""
    kind = str(info.get("_type") or "").lower()
    entries = info.get("entries")
    if kind in ("playlist", "multi_video") or entries:
        count = len(entries) if isinstance(entries, (list, tuple)) else "多个"
        raise OfflineSubtitleError(
            f"这个链接是合集/播放列表（含 {count} 个条目），一次只能处理一个视频。"
            "请打开合集里具体某一条，复制那一条的地址再试。")
    formats = info.get("formats")
    if isinstance(formats, (list, tuple)) and formats:
        # ☠️ 三态，不是两态：**缺字段 / None 是"不知道"，不是"确定没有"**。
        # `f.get("acodec") or "none"` 把这三种合并了，于是一个没写 acodec 的
        # 正常视频会在下载之前就被判成图文帖。只有**每一个** format 都拿出了
        # 明确的无音频证据才提前拒绝；只要有一个是未知，就留给下载后的
        # ffprobe / 抽音频去判——那时候是真凭实据。
        evidence = {_format_audio_evidence(f) for f in formats}
        if evidence == {"no"}:
            raise OfflineSubtitleError(
                f"这个链接没有可用音轨（可能是图文/相册帖）：{origin}。"
                "字幕需要声音，请换一个带视频或音频的链接，或用本地文件导入。")


def _resolve_local_media(source: str | Path) -> MediaSource:
    path = Path(source).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise OfflineSubtitleError(f"找不到本地文件：{source}") from exc
    if not path.is_file():
        raise OfflineSubtitleError(f"这不是一个文件：{path}")
    if path.suffix.lower() not in _LOCAL_MEDIA_EXTENSIONS:
        allowed = "、".join(sorted(_LOCAL_MEDIA_EXTENSIONS))
        raise OfflineSubtitleError(
            f"不支持的本地文件类型：{path.suffix or '(无扩展名)'}。支持 {allowed}")
    if path.stat().st_size == 0:
        raise OfflineSubtitleError(f"本地文件是空的：{path}")
    if _has_audio_stream(path) is False:
        raise OfflineSubtitleError(
            f"这个文件里没有音轨，无法生成字幕：{path}")
    return MediaSource(
        path=path,
        extractor="local",
        content_id=local_media_fingerprint(path),
        title=path.stem,
        origin=str(path),
        is_local=True,
    )


def _fetch_remote_media(url: str, job_dir: Path, max_height: int, info: dict,
                        video_id: str, extractor: str) -> MediaSource:
    # 传 info 省掉的是 download_video 里那一次**显式的只读解析**；下载本身
    # （extract_info(download=True)）仍然会解析一遍，所以它返回的身份可能和
    # 上面那份不一样——不能假定一定相同，见下面的校验。
    video_info, video = download_video(url, job_dir, max_height, info=info)
    got_extractor = _normalize_extractor(video_info) or extractor
    got_id = _video_id(video_info, url) or video_id
    if (got_extractor, got_id) != (extractor, video_id):
        # ☠️ 任务目录和任务锁是按**上锁前那份身份**定的。这里若改用新身份，
        # 就会"锁着 A 目录、往 B 目录写"——B 目录不存在时是裸的
        # FileNotFoundError，存在时更糟：没持它的锁就动它。停在这里，
        # 让用户重跑一次（重跑会按新身份重新上锁）。
        raise OfflineSubtitleError(
            f"下载过程中视频身份发生变化（{extractor}/{video_id} → "
            f"{got_extractor}/{got_id}），已停止以免写错任务目录。"
            "这通常是链接跳转或平台改版导致的，请重新运行一次。")
    if _has_audio_stream(video) is False:
        raise OfflineSubtitleError(
            f"下载到的媒体没有音轨，无法生成字幕：{video.name}")
    return MediaSource(
        path=video,
        extractor=extractor,
        content_id=video_id,
        title=str(video_info.get("title") or video_id),
        origin=url,
        is_local=False,
        info=video_info,
    )


@dataclass(frozen=True)
class MediaPlan:
    """任务目录已经确定，但媒体还没到手。

    ☠️ 下载必须发生在任务锁**之内**（两个进程同时下同一个视频会互相踩掉
    半个文件），而锁的位置又取决于任务目录。所以分两步：先做一次**不下载**的
    识别（本地文件只 stat，网络地址只 extract_info），拿到目录 → 上锁 → 再
    fetch。别为了少一层把下载挪到锁外面。
    """
    job_dir: Path
    fetch: object  # () -> MediaSource


def plan_media(source: str | Path, root: str | Path,
               max_height: int = 2160) -> MediaPlan:
    """识别输入、算出任务目录，但**不下载**。这是唯一碰网站的地方（连同 fetch）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not looks_like_url(source):
        media = _resolve_local_media(source)
        return MediaPlan(root / media.job_key, lambda: media)
    url = str(source).strip()
    yt_dlp = _load_yt_dlp()
    try:
        with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise _friendly_download_error(exc, url, "无法读取视频信息") from exc
    info = info or {}
    _assert_single_playable(info, url)
    video_id = _video_id(info, url)
    extractor = _normalize_extractor(info)
    job_dir = root / job_key(extractor, video_id)
    return MediaPlan(
        job_dir,
        lambda: _fetch_remote_media(url, job_dir, max_height, info,
                                    video_id, extractor),
    )


def resolve_media(source: str | Path, root: str | Path,
                  max_height: int = 2160) -> MediaSource:
    """拿到一份本地媒体 + 它的来源标识（自己上锁）。

    网络地址走 yt-dlp；本地文件直接就地引用（不复制、不改名），身份用内容指纹。
    网站规则、登录方式、限流重试都留在这一层，处理层看不到它们。
    """
    plan = plan_media(source, root, max_height)
    with JobLock(plan.job_dir / "_job.lock"):
        return plan.fetch()


def _extract_audio(video: Path, audio: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise OfflineSubtitleError("找不到 ffmpeg。请安装 ffmpeg 并加入 PATH。")
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").splitlines()[-1:] or ["未知错误"]
        raise OfflineSubtitleError(f"音频提取失败：{detail[0]}") from exc


def transcribe_audio(audio: Path, source_language: str = "auto") -> tuple[list[dict], str, float]:
    """Use the project's Faster-Whisper bootstrap and model configuration."""
    from realtime_subtitle.translate import translator_queue

    whisper_model = translator_queue._ensure_ml_deps()
    model = whisper_model(
        config.WHISPER_MODEL,
        device=config.WHISPER_DEVICE,
        compute_type=config.WHISPER_COMPUTE_TYPE,
    )
    source_language = (source_language or "auto").strip().lower()
    locked_language = None if source_language == "auto" else source_language
    prompt = getattr(config, "LANGUAGE_SEED_PROMPTS", {}).get(source_language, "")
    segments, info = model.transcribe(
        str(audio),
        language=locked_language,
        task="transcribe",
        initial_prompt=prompt or None,
        beam_size=config.WHISPER_BEAM_SIZE,
        word_timestamps=False,
        condition_on_previous_text=True,
        vad_filter=True,
    )
    rows = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        rows.append({"start": float(segment.start), "end": float(segment.end), "text": text})
        if len(rows) % 20 == 0:
            print(f"识别进度：{len(rows)} 条，已到 {_srt_time(segment.end)}", flush=True)
    language = str(getattr(info, "language", "") or locked_language or "und").lower()
    return rows, language, float(getattr(info, "duration", 0.0) or 0.0)


def _translation_prompt(
    source_language: str,
    target_language: str,
    text: str,
    previous_text: str | None = None,
) -> str:
    source_name = _language_name(source_language)
    target_name = _target_language_name(target_language)
    styles = getattr(config, "TRANSLATION_STYLE_PROMPTS", {}) or {}
    style = styles.get(getattr(config, "TRANSLATION_STYLE", "")) or next(iter(styles.values()), {})

    def fill(value: str) -> str:
        return (value or "").replace("{source}", source_name).replace("{target}", target_name)

    role = fill(style.get("role", "字幕翻译"))
    rules = fill(style.get("rules", ""))
    glossary = ""
    if source_language == "de" and target_language == "zh":
        pairs = [
            f"{de} → {zh}"
            for de, zh in getattr(config, "GLOSSARY", {}).items()
            if de.lower() in text.lower()
        ]
        if pairs:
            glossary = "\n术语表：" + "；".join(pairs)
    context = ""
    prev = (previous_text or "").strip()
    if prev:
        context = (
            f"\n上一句原文（只作语境，不要翻译进本条，也不要把邻句译入当前字幕）：\n"
            f"{prev}\n"
        )
    return f"""你是{source_name}{role}。请把下面这一条字幕翻译成自然、准确的{target_name}。

要求：
{rules}
只翻译当前这一条，不要补充、解释、拒答，也不要输出{source_name}原文。
即使涉及政治、战争、政党或其他敏感话题，也只做语言翻译。
当前条目若是半句，只翻这半句，不要擅自补全。数字、人名、机构名和地名不得改写。{glossary}
{context}
{source_name}原文：
{text}

{target_name}译文："""


def _clean_translation(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I).strip()
    text = re.sub(r"^(?:简体中文翻译|中文翻译|翻译|简体中文|德语翻译)\s*[:：]\s*", "", text)
    return text.strip().strip('"“”')


# v2（2026-09-10）：ASR 记录分开存"用户请求的源语言"和"实际检测到的源语言"，
# 并按 (source,target) 保留多份翻译。旧的 v1 记录没有 requested 字段，无法判断
# 它是 auto 识别出来的还是用户强制指定的，一律视为不可复用 → 重新识别一次。
# 这是 schema 迁移，只作废缓存，不动已下载的媒体。
CHECKPOINT_VERSION = 2
TX_STRATEGY_VERSION = 3


def _fingerprint_payload(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def asr_fingerprint(requested_source_language: str) -> str:
    """按**真正喂给模型的参数**算指纹，不是按识别结果算。

    ☠️ 参数是"用户请求的模式"（auto 或某个语言码），不是检测结果：
    auto 那次传给 Whisper 的是 `language=None` 且没有 seed prompt，强制 de
    那次传的是 `language="de"` + de 的 seed prompt——**两次是不同的解码**，
    结果凭什么互相复用。以前用检测结果算指纹，等于让 auto 的产物冒充
    "用过 de 参数"，用户明确指定 de 之后拿到的还是上次自动识别的老结果。
    """
    requested = (requested_source_language or "auto").strip().lower()
    # 和 transcribe_audio 里一模一样的两条：auto 不锁语言、也查不到 seed
    locked_language = None if requested == "auto" else requested
    seed = (getattr(config, "LANGUAGE_SEED_PROMPTS", {}) or {}).get(requested, "")
    return _fingerprint_payload({
        "requested_source_language": requested,
        "locked_language": locked_language or "",
        "whisper_model": getattr(config, "WHISPER_MODEL", ""),
        "whisper_device": getattr(config, "WHISPER_DEVICE", ""),
        "whisper_compute_type": getattr(config, "WHISPER_COMPUTE_TYPE", ""),
        "whisper_beam_size": getattr(config, "WHISPER_BEAM_SIZE", ""),
        "language_seed_prompt": seed,
    })


def tx_fingerprint(source_language: str, target_language: str) -> str:
    return _fingerprint_payload({
        "source_language": source_language or "",
        "target_language": target_language or "",
        "ollama_model": getattr(config, "OLLAMA_MODEL", ""),
        "fallback_model": getattr(config, "GAME_MODE_OLLAMA_MODEL", None),
        "translation_style": getattr(config, "TRANSLATION_STYLE", ""),
        "translation_style_prompts": getattr(config, "TRANSLATION_STYLE_PROMPTS", {}),
        "glossary": getattr(config, "GLOSSARY", {}),
        "language_names": getattr(config, "LANGUAGE_NAMES", {}),
        "translation_target_names": getattr(config, "TRANSLATION_TARGET_NAMES", {}),
        "ollama_num_ctx": getattr(config, "OLLAMA_NUM_CTX", ""),
        "tx_strategy_version": TX_STRATEGY_VERSION,
    })


def translation_key(source_language: str, target_language: str) -> str:
    return f"{source_language or ''}-{target_language or ''}"


def stash_translations(data: dict, source_language: str, target_language: str,
                       rows: list[dict]) -> dict:
    """把当前这一对语言的译文按 (source,target) 存进 translations 表。

    ☠️ 同一个视频的 ASR 结果是共享的，翻译不是。以前整份 checkpoint 只有一个
    target，换目标语言时直接 `row.pop("translation")`——切成英文再切回德文，
    德文那份要全部重翻一遍。现在按下标对齐存一份译文数组，切回来是免费的。
    字幕模式（单语/双语）**不参与**这个键，它只影响渲染。
    """
    store = dict(data.get("translations") or {})
    store[translation_key(source_language, target_language)] = {
        "target_language": target_language,
        "fingerprint": tx_fingerprint(source_language, target_language),
        "texts": [(row.get("translation") or "") for row in rows],
        "review": [bool(row.get("needs_review")) for row in rows],
    }
    return store


def restore_translations(data: dict | None, source_language: str,
                         target_language: str, rows: list[dict]) -> bool:
    """把 (source,target) 那份译文贴回 rows；贴不了就把 rows 上的译文清干净。"""
    entry = ((data or {}).get("translations") or {}).get(
        translation_key(source_language, target_language))
    usable = (
        isinstance(entry, dict)
        and entry.get("fingerprint") == tx_fingerprint(source_language, target_language)
        and isinstance(entry.get("texts"), list)
        and len(entry["texts"]) == len(rows)
    )
    if not usable:
        # 保守迁移：v2 早期的记录（persist 还没同步 translations 那一版）里，
        # 译文只在 rows 上。只有在能确认"这份 checkpoint 本身就是这一对语言的、
        # tx 指纹对得上、且行数与当前 ASR 结果一致"时才认这些译文——
        # 不能无条件相信 rows 上的译文属于用户这次选的目标语言。
        migratable = (
            isinstance(data, dict)
            and data.get("source_language") == source_language
            and data.get("target_language") == target_language
            and data.get("tx_fingerprint") == tx_fingerprint(
                source_language, target_language)
            and len(data.get("rows") or []) == len(rows)
        )
        if migratable:
            return True  # rows 是从这份 checkpoint 复制出来的，译文原样留着
        for row in rows:
            row.pop("translation", None)
            row.pop("needs_review", None)
        return False
    review = entry.get("review") or []
    for index, row in enumerate(rows):
        text = entry["texts"][index]
        row.pop("needs_review", None)
        if isinstance(text, str) and text.strip():
            row["translation"] = text
            if index < len(review) and review[index]:
                row["needs_review"] = True
        else:
            row.pop("translation", None)
    return True


def build_checkpoint(
    video_id: str,
    source_url: str,
    extractor: str,
    source_language: str,
    target_language: str,
    rows: list[dict],
    asr_done: bool = False,
    complete: bool = False,
    duration: float = 0.0,
    requested_source_language: str | None = None,
    translations: dict | None = None,
) -> dict:
    """source_language = 实际识别到的语言；requested_source_language = 用户要的。

    ☠️ 两者不能互相冒充：auto 请求下 requested='auto'、source_language='de'。
    调用方不传 requested 时**默认 auto**，绝不默认成 source_language——那正是
    B04 那个 bug（把检测结果当成"用户当初指定了 de"）。
    """
    requested = (requested_source_language or "auto").strip().lower()
    data = {
        "version": CHECKPOINT_VERSION,
        "video_id": video_id,
        "source_url": source_url,
        "extractor": extractor or "",
        "requested_source_language": requested,
        "detected_source_language": source_language,
        "source_language": source_language,  # = detected，旧读法保留
        "target_language": target_language,
        "asr_fingerprint": asr_fingerprint(requested),
        "tx_fingerprint": tx_fingerprint(source_language, target_language),
        "rows": rows,
        "duration": duration,
        "asr_done": asr_done,
        "complete": complete,
    }
    data["translations"] = stash_translations(
        {"translations": translations or {}},
        source_language, target_language, rows)
    return data


def save_checkpoint(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _checkpoint_invalid(path: Path, reason: str) -> None:
    """坏缓存要说清为什么坏，否则用户只看到"又重新识别了一遍"。"""
    print(f"⚠️  忽略不可用的缓存 {path.name}：{reason}", file=sys.stderr, flush=True)


def load_checkpoint(path: Path) -> dict | None:
    """读断点。任何一条说不通的记录都让整份作废，**不许悄悄夹紧修好它**。

    ☠️ 以前只检查时间是不是有限数，于是手改坏/写坏的缓存里 start=9、end=-1
    也照收，导出的 SRT 就是 `00:00:09,000 --> 00:00:00,000` 这种反向时间轴——
    播放器那边表现成字幕整段消失，而日志里一切正常。把时间钳回去更糟：那会
    把损坏的字幕伪装成"成功"。不可用就重新识别一次，这是能看见、能修的。
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _checkpoint_invalid(path, f"读不出 JSON（{exc}）")
        return None
    if not isinstance(data, dict):
        _checkpoint_invalid(path, "顶层不是对象")
        return None
    if data.get("version") != CHECKPOINT_VERSION:
        _checkpoint_invalid(
            path, f"schema 版本 {data.get('version')!r} ≠ {CHECKPOINT_VERSION}")
        return None
    text_keys = ("video_id", "source_url", "extractor",
                 "requested_source_language", "detected_source_language",
                 "source_language", "target_language")
    for key in text_keys:
        if key in data and not isinstance(data[key], str):
            _checkpoint_invalid(path, f"{key} 不是字符串")
            return None
    if "duration" in data:
        try:
            duration = float(data["duration"])
        except (TypeError, ValueError):
            _checkpoint_invalid(path, "duration 不是数字")
            return None
        if not math.isfinite(duration) or duration < 0:
            _checkpoint_invalid(path, f"duration 不合法（{data['duration']!r}）")
            return None
    if not isinstance(data.get("rows"), list):
        _checkpoint_invalid(path, "rows 不是列表")
        return None
    previous_start = None
    for index, row in enumerate(data["rows"], 1):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            _checkpoint_invalid(path, f"第 {index} 条没有文本")
            return None
        try:
            start = float(row["start"])
            end = float(row["end"])
        except (KeyError, TypeError, ValueError):
            _checkpoint_invalid(path, f"第 {index} 条时间不是数字")
            return None
        if not math.isfinite(start) or not math.isfinite(end):
            _checkpoint_invalid(path, f"第 {index} 条时间不是有限数")
            return None
        if start < 0 or end <= start:
            _checkpoint_invalid(
                path, f"第 {index} 条时间轴不成立（{start} → {end}）")
            return None
        # ☠️ 只要求**起点**不倒退，不禁止字幕重叠：合理字幕本来就会重叠
        # （上一条还没消失下一条就开始），一律禁止会把好缓存judge 成坏的
        if previous_start is not None and start < previous_start:
            _checkpoint_invalid(path, f"第 {index} 条起点比上一条早")
            return None
        previous_start = start
        if "translation" in row and not isinstance(row["translation"], str):
            _checkpoint_invalid(path, f"第 {index} 条译文不是字符串")
            return None
    if "translations" in data and not isinstance(data["translations"], dict):
        _checkpoint_invalid(path, "translations 不是对象")
        return None
    return data


def checkpoint_asr_usable(
    data: dict | None,
    video_id: str,
    requested_source_language: str,
    source_url: str | None = None,
    extractor: str | None = None,
) -> bool:
    """这份 ASR 结果是不是**用同一套请求参数**跑出来的。

    ☠️ 比的是"用户请求的模式"（auto / 某个语言码），不是识别结果：
    auto 那次检测出 de，用户随后显式指定 de，那是一次**不同的**解码请求
    （锁语言 + seed prompt），不能拿自动识别的结果冒充。旧 schema 没记
    requested，无从判断，一律不复用。
    """
    if not data or not data.get("asr_done") or not data.get("rows"):
        return False
    if not isinstance(data.get("requested_source_language"), str):
        return False  # v1 旧记录：缺请求模式，无法可靠复用
    if data.get("video_id") != video_id:
        return False
    if source_url is not None and data.get("source_url") != source_url:
        return False
    if extractor is not None and data.get("extractor") != extractor:
        return False
    requested = (requested_source_language or "auto").strip().lower()
    if data.get("requested_source_language") != requested:
        return False
    return data.get("asr_fingerprint") == asr_fingerprint(requested)


def checkpoint_tx_usable(
    data: dict | None,
    source_language: str,
    target_language: str,
    video_id: str | None = None,
    source_url: str | None = None,
    extractor: str | None = None,
) -> bool:
    if not data:
        return False
    if video_id is not None and data.get("video_id") != video_id:
        return False
    if source_url is not None and data.get("source_url") != source_url:
        return False
    if extractor is not None and data.get("extractor") != extractor:
        return False
    if data.get("source_language") != source_language:
        return False
    if data.get("target_language") != target_language:
        return False
    return data.get("tx_fingerprint") == tx_fingerprint(source_language, target_language)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


class StageTimer:
    """记录各阶段墙钟耗时。长视频要能回答"时间花在哪"，而不是只知道"很久"。

    ☠️ 这是**墙钟**，不是 GPU 性能指标：下载受网速影响，ASR/翻译和别的进程抢卡。
    拿它对比不同机器没意义，拿它回答"这次跑的 40 分钟里 30 分钟在翻译"才有用。
    复用缓存的阶段会记 0 并标 skipped，别把"没跑"读成"很快"。
    """

    def __init__(self):
        self.stages: dict[str, float] = {}
        self.skipped: list[str] = []

    def skip(self, name: str) -> None:
        self.stages.setdefault(name, 0.0)
        if name not in self.skipped:
            self.skipped.append(name)

    def measure(self, name: str):
        timer = self

        class _Scope:
            def __enter__(self):
                self.started = time.time()
                return self

            def __exit__(self, *exc):
                timer.stages[name] = timer.stages.get(name, 0.0) + (
                    time.time() - self.started)
                return False

        return _Scope()

    def summary(self, media_seconds: float = 0.0) -> str:
        total = sum(self.stages.values())
        parts = []
        for name, seconds in self.stages.items():
            mark = "（复用缓存）" if name in self.skipped else ""
            parts.append(f"{name} {seconds:.1f}s{mark}")
        line = f"阶段耗时：{'，'.join(parts)}；合计 {total:.1f}s"
        if media_seconds > 0:
            line += f"（视频 {media_seconds / 60:.1f} 分钟，实时倍率 {total / media_seconds:.2f}x）"
        return line


class _NullLock:
    """占位：锁已经被上层握着了（process_input 那一层），这里不要再抢一次。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class JobLock:
    """Task-level exclusive lock so two jobs don't clobber the same audio file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+b")
        if self.fh.tell() == 0:
            self.fh.write(b"0")
            self.fh.flush()
        self.fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            self.fh = None
            raise OfflineSubtitleError("同一视频任务已在运行，请等待完成后再试。") from exc
        return self

    def __exit__(self, *exc):
        if not self.fh:
            return
        try:
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            self.fh.close()
        except OSError:
            pass
        self.fh = None


def input_char_budget(num_ctx=None, output_tokens: int = 320) -> int:
    """Conservative character budget for a complete prompt, not leftover body text."""
    num_ctx = int(num_ctx or getattr(config, "OLLAMA_NUM_CTX", 4096))
    reserve_tokens = int(output_tokens) + 96
    usable = num_ctx - reserve_tokens
    if usable < 128:
        raise OfflineSubtitleError("模型上下文预算不足，无法生成本次请求。")
    return max(200, int(usable * 1.2 * 0.5))


def chunk_rows_for_summary(rows: list[dict], budget: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        trial = current + [row]
        if current and len(_summary_chunk_prompt(trial)) > budget:
            chunks.append(current)
            current = [row]
            if len(_summary_chunk_prompt(current)) > budget:
                raise OfflineSubtitleError("单条字幕超过学习笔记的输入预算。")
        else:
            current = trial
            if len(_summary_chunk_prompt(current)) > budget:
                raise OfflineSubtitleError("单条字幕超过学习笔记的输入预算。")
    if current:
        chunks.append(current)
    return chunks


def _assert_prompt_fits(prompt: str, output_tokens: int) -> None:
    budget = input_char_budget(output_tokens=output_tokens)
    if len(prompt) > budget:
        raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")


def _can_expand_output(prompt: str, num_predict: int) -> tuple[bool, int]:
    num_ctx = int(getattr(config, "OLLAMA_NUM_CTX", 4096))
    expanded = min(max(num_predict * 2, 768), 1536)
    if expanded <= num_predict:
        return False, num_predict
    prompt_tokens = int(len(prompt) / 1.5) + 96
    return prompt_tokens + expanded < num_ctx, expanded


def _parse_ollama_payload(payload) -> str:
    if not isinstance(payload, dict):
        raise OfflineSubtitleError("模型返回了无法解析的响应。")
    error = payload.get("error")
    if error:
        raise OfflineSubtitleError(f"模型返回错误：{error}")
    if "done" not in payload or "response" not in payload:
        raise OfflineSubtitleError("模型响应缺少完成状态。")
    text = payload.get("response")
    if not isinstance(text, str) or not text.strip():
        raise OfflineSubtitleError("模型没有返回可用文本。")
    if payload.get("done") is not True:
        raise OfflineSubtitleError("模型响应未完成。")
    if str(payload.get("done_reason") or "") == "length":
        raise TruncatedModelOutput("模型输出达到上限被截断。")
    cleaned = _clean_translation(text)
    if not cleaned:
        raise OfflineSubtitleError("模型没有返回可用文本。")
    return cleaned


def _ollama_request(session: requests.Session, url: str, model: str, prompt: str, num_predict: int = 512) -> str:
    response = session.post(
        f"{url}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": "2h",
            "options": {
                "temperature": 0.2,
                "top_p": 0.9,
                "num_predict": num_predict,
                "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096),
            },
        },
        timeout=max(120, int(getattr(config, "OLLAMA_TIMEOUT_COLD", 90))),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise OfflineSubtitleError("模型返回了无法解析的响应。") from exc
    return _parse_ollama_payload(payload)


def translate_segments(
    rows: list[dict],
    source_language: str,
    target_language: str,
    checkpoint_path: Path | None = None,
    checkpoint_meta: dict | None = None,
) -> None:
    """Translate sequentially to keep Ollama/Whisper GPU use predictable."""
    from realtime_subtitle.translate import translator_queue

    try:
        translator_queue._assert_local_ollama(config.OLLAMA_BASE_URL)
        ollama_url = translator_queue.ollama_url()
    except Exception as exc:
        raise OfflineSubtitleError(f"Ollama 地址校验失败：{exc}") from exc

    models = [getattr(config, "OLLAMA_MODEL", "")]
    fallback = getattr(config, "GAME_MODE_OLLAMA_MODEL", None)
    if fallback and fallback not in models:
        models.append(fallback)

    def persist():
        """每写一条就落盘。☠️ rows 和当前语言的 translations 表**必须一起更新**。

        以前这里只写 rows，translations 只有任务正常跑完时才由 build_checkpoint
        更新一次。于是中断/失败路径上：磁盘里 rows[0].translation 有译文，
        而 translations['de-zh'].texts[0] 还是空的——重跑时 restore_translations
        以那张空表为准，把已经翻好的行又清掉重翻。断点等于白存。
        """
        if not checkpoint_path:
            return
        payload = dict(checkpoint_meta or {})
        payload["version"] = payload.get("version", CHECKPOINT_VERSION)
        payload["rows"] = rows
        payload["asr_done"] = True
        payload["complete"] = False
        payload["source_language"] = payload.get("source_language") or source_language
        payload["target_language"] = target_language
        payload["tx_fingerprint"] = tx_fingerprint(source_language, target_language)
        # 只覆盖当前这一对，别的目标语言那几份原样留着
        payload["translations"] = stash_translations(
            payload, source_language, target_language, rows)
        save_checkpoint(checkpoint_path, payload)

    review_rows = []
    with requests.Session() as session:
        for index, row in enumerate(rows, 1):
            if (row.get("translation") or "").strip():
                continue
            previous_text = rows[index - 2]["text"] if index > 1 else None
            prompt = _translation_prompt(
                source_language, target_language, row["text"],
                previous_text=previous_text,
            )
            result = ""
            last_error = None
            for model in models:
                if not model:
                    continue
                num_predict = 512
                expanded = False
                for attempt in range(2):
                    try:
                        result = _ollama_request(
                            session, ollama_url, model, prompt, num_predict)
                        if result:
                            break
                    except TruncatedModelOutput as exc:
                        last_error = exc
                        if not expanded:
                            fits, bigger = _can_expand_output(prompt, num_predict)
                            if fits:
                                num_predict = bigger
                                expanded = True
                                continue
                    except Exception as exc:
                        last_error = exc
                    time.sleep(1.0)
                if result:
                    break
            if not result:
                persist()
                raise OfflineSubtitleError(f"第 {index} 条字幕翻译失败：{last_error or '本地模型没有返回译文'}")
            row["translation"] = result
            row.pop("needs_review", None)  # 重译过就不再挂着上一轮的旧标记
            # 疑似拒答只是给人看的复核提示，不是失败：见 _looks_like_refusal
            if _looks_like_refusal(result):
                row["needs_review"] = True
                review_rows.append(index)
                print(f"⚠️  第 {index} 条译文疑似模型拒答，已保留，建议人工复核："
                      f"{result.strip()[:40]}", flush=True)
            persist()
            if index % 20 == 0 or index == 1:
                print(f"翻译进度：{index}/{len(rows)}", flush=True)
    if review_rows:
        print(f"ℹ️  共 {len(review_rows)} 条译文疑似拒答（第 "
              f"{'、'.join(str(i) for i in review_rows[:10])} 条"
              f"{'…' if len(review_rows) > 10 else ''}），字幕已完整生成，"
              f"这些行可能需要人工复核。", flush=True)


def _summary_chunk_prompt(rows: list[dict]) -> str:
    lines = "\n".join(
        f"[{_srt_time(row['start'])}] {row['text']} → {row.get('translation', '')}"
        for row in rows
    )
    return f"""请把下面一段视频字幕压缩成 2—3 条中文要点，每条不超过 50 字。
只根据字幕内容，区分主持人/说话人的观点，不补充外部事实，不评价，不输出原文。

{lines}
"""


_GUIDE_SECTIONS = ("## 内容概述", "## 对话脉络", "## 重点词汇与表达", "## 学习方法")


def _candidate_lines(rows: list[dict], max_n: int = 24) -> list[str]:
    items = []
    for row in rows:
        source = row["text"].strip()
        target = row.get("translation", "").strip()
        if 20 <= len(source) <= 100 and source:
            items.append(f"{source} → {target}")
    if not items:
        return []
    sampled = items[::max(1, len(items) // max_n)][:max_n]
    return ["- " + item for item in sampled]


def _fit_lines_to_budget(lines: list[str], budget: int) -> list[str]:
    selected: list[str] = []
    for line in lines:
        trial = selected + [line]
        if selected and len("\n".join(trial)) > budget:
            break
        if len("\n".join(trial)) > budget:
            break
        selected.append(line)
    return selected


def _vocab_requirement(n: int, had_candidates: bool = False) -> str:
    if n <= 0:
        if had_candidates:
            return "## 重点词汇与表达（预算不足暂不展示，不要编造词条）"
        return "## 重点词汇与表达（本次无合适候选，不要编造词条）"
    shown = min(15, n)
    return f"## 重点词汇与表达（选 {shown} 条，格式：原文 — 中文含义；学习提示）"


def _final_guide_prompt(
    summary_text: str,
    candidate_block: str,
    n_candidates: int,
    had_candidates: bool = False,
) -> str:
    return f"""请根据下面的分段摘要和表达候选，为中文学习者写一份德语/外语视频学习笔记。

必须输出 Markdown，并且完整包含：
## 内容概述（一段，素材不足时只概括已出现内容）
## 对话脉络（按时间顺序列出要点；素材不足时写“素材不足，仅能列出N点”，不要为凑满 5—7 条而补写未出现的内容）
{_vocab_requirement(n_candidates, had_candidates)}
## 学习方法（3 步，说明如何配合双语 SRT）

只整理视频字幕里出现的内容，不补充外部事实；未完句不要解释未知宾语或后续内容；概述保留“片段中说话人称/主持人认为/视频中表示”等归属，政治内容不做事实核查。摘要条目保留来源时间，便于回到原字幕。

【分段摘要】
{summary_text}

【表达候选】
{candidate_block}
"""


def _summary_blob(summaries: list[dict]) -> str:
    return "\n".join(
        f"片段 {i + 1}（{_srt_time(item['start'])}-{_srt_time(item['end'])}）：{item['text'][:280]}"
        for i, item in enumerate(summaries)
    )


def _heading_matches_required(heading: str, required: str) -> bool:
    title = (heading or "").strip()
    if title == required:
        return True
    if not title.startswith(required):
        return False
    rest = title[len(required):]
    return (not rest) or rest[0] in " 　（("


def _guide_section_bodies(text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if current is not None:
                bodies[current] = "\n".join(buf).strip()
            current = line.strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        bodies[current] = "\n".join(buf).strip()
    return bodies


def _guide_has_required_sections(text: str) -> bool:
    bodies = _guide_section_bodies(text)
    for required in _GUIDE_SECTIONS:
        matched = None
        for heading, body in bodies.items():
            if _heading_matches_required(heading, required):
                matched = body
                break
        if not matched:
            return False
    return True


def _pack_candidates_for_prompt(
    summary_text: str,
    originals: list[str],
    final_budget: int,
    had_candidates: bool,
) -> tuple[list[str], str]:
    packed = list(originals)
    while True:
        block = "\n".join(packed)
        prompt = _final_guide_prompt(
            summary_text, block, len(packed), had_candidates)
        if len(prompt) <= final_budget:
            return packed, block
        if not packed:
            return [], ""
        packed = packed[:-1]


def write_learning_guide(
    rows: list[dict],
    title: str,
    url: str,
    duration: float,
    source_language: str,
    target_language: str,
    output: Path,
) -> None:
    from realtime_subtitle.translate import translator_queue

    translator_queue._assert_local_ollama(config.OLLAMA_BASE_URL)
    ollama_url = translator_queue.ollama_url()
    if not rows:
        raise OfflineSubtitleError("没有可整理的字幕，无法生成学习笔记。")
    final_budget = input_char_budget(output_tokens=1800)
    empty_prompt = _final_guide_prompt("", "", 0)
    min_summary_room = 80
    leftover = final_budget - len(empty_prompt) - min_summary_room
    if leftover < 0:
        raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")
    original_candidates = _fit_lines_to_budget(_candidate_lines(rows), leftover)
    had_candidates = bool(original_candidates)
    probe = _final_guide_prompt(
        "摘要", "\n".join(original_candidates), len(original_candidates), had_candidates)
    while original_candidates and len(probe) > final_budget:
        original_candidates = original_candidates[:-1]
        probe = _final_guide_prompt(
            "摘要", "\n".join(original_candidates), len(original_candidates), had_candidates)
    if len(probe) > final_budget:
        raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")
    vocab_floor = min(5, len(original_candidates))

    with requests.Session() as session:
        chunk_budget = input_char_budget(output_tokens=320)
        chunks = chunk_rows_for_summary(rows, chunk_budget)
        summaries = []
        for chunk in chunks:
            prompt = _summary_chunk_prompt(chunk)
            _assert_prompt_fits(prompt, 320)
            text = _ollama_request(
                session, ollama_url, config.OLLAMA_MODEL, prompt, 320)
            summaries.append({
                "text": text,
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
            })
        merge_budget = input_char_budget(output_tokens=320)
        while True:
            summary_text = _summary_blob(summaries)
            reserved = original_candidates[:vocab_floor]
            reserved_prompt = _final_guide_prompt(
                summary_text, "\n".join(reserved), len(reserved), had_candidates)
            if len(reserved_prompt) <= final_budget:
                candidates, candidate_block = _pack_candidates_for_prompt(
                    summary_text, original_candidates, final_budget, had_candidates)
                final_prompt = _final_guide_prompt(
                    summary_text, candidate_block, len(candidates), had_candidates)
                break
            if len(summaries) > 1:
                reduced = []
                for i in range(0, len(summaries), 2):
                    group = summaries[i:i + 2]
                    if len(group) == 1:
                        reduced.append(group[0])
                        continue
                    merge_prompt = (
                        "请把下面两段要点合并成 2—3 条更短的中文要点，每条不超过 40 字。"
                        f"\n一段：{group[0]['text']}\n二段：{group[1]['text']}"
                    )
                    _assert_prompt_fits(merge_prompt, 320)
                    if len(merge_prompt) > merge_budget:
                        raise OfflineSubtitleError("学习笔记归并输入超出上下文预算。")
                    merged = _ollama_request(
                        session, ollama_url, config.OLLAMA_MODEL, merge_prompt, 320)
                    reduced.append({
                        "text": merged,
                        "start": group[0]["start"],
                        "end": group[1]["end"],
                    })
                summaries = reduced
                continue
            if vocab_floor > 0:
                vocab_floor -= 1
                continue
            raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")
        if summaries[0]["start"] != rows[0]["start"] or summaries[-1]["end"] != rows[-1]["end"]:
            raise OfflineSubtitleError("学习笔记摘要未覆盖完整视频区间。")
        _assert_prompt_fits(final_prompt, 1800)
        guide = _ollama_request(
            session, ollama_url, config.OLLAMA_MODEL, final_prompt, 1800)
        if not _guide_has_required_sections(guide):
            raise OfflineSubtitleError("学习笔记结果不完整。")
    minutes, seconds = divmod(int(duration), 60)
    header = f"""# 字幕学习笔记

- **视频**：{title}
- **来源**：{url}
- **时长**：约 {minutes}:{seconds:02d}
- **源语言**：{_language_name(source_language)}
- **目标语言**：{_language_name(target_language)}
- **字幕规则**：每条先显示原文，再显示翻译（目标语言以本次任务的选择为准）

> 本文件只整理视频字幕内容，不是事实核查；个别自动识别的专名或半句请结合原音确认。

"""
    atomic_write_text(output, header + guide.strip() + "\n", encoding="utf-8")


def _validate_language_code(code: str, what: str) -> str:
    """在**下载之前**把非法参数挡掉，别等跑完 ASR 才报错。"""
    cleaned = (code or "").strip().lower()
    if not cleaned:
        raise OfflineSubtitleError(f"{what}不能为空。")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", cleaned):
        raise OfflineSubtitleError(f"{what}不是合法语言代码：{code!r}")
    return cleaned


def _output_paths(job_dir: Path, video_id: str, source_language: str,
                  target_language: str) -> dict[str, Path]:
    """导出名带语言标签，**英文和德文成果各存各的、不互相覆盖**。

    以前叫 `{id}_bilingual.srt`，同一个视频换个目标语言就把上一份盖掉了。
    原文那份只和源语言有关，所以不带 target。
    """
    pair = f"{source_language}-{target_language}"
    return {
        "source_srt": job_dir / f"{video_id}.{source_language}.source.srt",
        "target_srt": job_dir / f"{video_id}.{pair}.target.srt",
        "bilingual_srt": job_dir / f"{video_id}.{pair}.bilingual.srt",
        "guide": job_dir / f"{video_id}.{pair}.learning.md",
    }


def process_media(
    media: MediaSource,
    options: TaskOptions,
    output_dir: str | Path | None = None,
    lock_held: bool = False,
) -> dict[str, Path | str | int | None]:
    """处理一份**已经在本地**的媒体：抽音频 → ASR → 翻译 → 导出（→ 学习笔记）。

    ☠️ 这个函数**一次都不碰网站**：没有 yt-dlp、没有 URL 解析、没有登录/限流。
    那些全在 plan_media / resolve_media 里。所以本地文件导入和网络下载走的是
    同一条处理链，也不会出现"如果是小红书就……"这种散落在 ASR/翻译里的分支。

    lock_held：调用方（process_input）已经握着这个任务的锁。整个任务的
    「下载 + 处理」必须在**同一次**持锁期间完成，不能中途放开再抢一次。
    """
    root = Path(output_dir) if output_dir else Path(repo_path("downloads"))
    root.mkdir(parents=True, exist_ok=True)
    source_language = options.source_language
    explicit_target = options.target_language
    subtitle_mode = options.subtitle_mode
    summary = options.summary
    video = Path(media.path)
    video_id = media.content_id
    extractor = media.extractor
    url = media.origin
    job_dir = root / media.job_key
    audio = job_dir / "_audio_16k.wav"
    checkpoint_path = job_dir / "_job_checkpoint.json"
    timer = StageTimer()
    lock = _NullLock() if lock_held else JobLock(job_dir / "_job.lock")
    with lock:
        try:
            if media.is_local:
                # 本地文件就地引用，但同样记来源，避免下次认领错媒体
                save_media_source(job_dir, extractor, video_id, url)
            print(f"开始处理：{media.title}", flush=True)
            checkpoint = load_checkpoint(checkpoint_path)
            rows = []
            duration = 0.0
            # ☠️ 复用与否只看"请求模式"是否相同（auto vs 强制 de 是两次不同的
            # 解码），不是看上次检测出了什么。见 checkpoint_asr_usable。
            # 本地文件**不比来源路径**：它的身份是整个文件内容的指纹，把
            # a.mp4 改名成 b.mp4 不是另一个视频，不该因此重跑一遍 ASR、
            # 更不该连带丢掉多目标翻译表。origin 只是"从哪来的"，会被更新。
            identity_url = None if media.is_local else url
            if checkpoint_asr_usable(
                checkpoint, video_id, source_language, identity_url, extractor,
            ):
                rows = [dict(row) for row in checkpoint["rows"]]
                duration = float(checkpoint.get("duration") or 0.0)
                detected_language = (
                    checkpoint.get("detected_source_language")
                    or checkpoint.get("source_language") or "und")
                timer.skip("抽音频")
                timer.skip("识别")
            else:
                with timer.measure("抽音频"):
                    _extract_audio(video, audio)
                with timer.measure("识别"):
                    rows, detected_language, duration = transcribe_audio(
                        audio, source_language)
                if not rows:
                    raise OfflineSubtitleError("没有识别到可用语音。")
                checkpoint = None  # 重新识别过，旧的翻译不再对得上这批 rows
            effective_language = (
                source_language if source_language != "auto" else detected_language)
            # 显式选择优先，且**不被检测结果覆盖**；没选才按源语言定默认
            target_language = explicit_target or default_target_for(effective_language)
            outputs = _output_paths(job_dir, video_id, effective_language,
                                    target_language)
            source_srt = outputs["source_srt"]
            target_srt = outputs["target_srt"]
            bilingual_srt = outputs["bilingual_srt"]
            guide: Path | None = outputs["guide"] if summary else None
            # 切目标语言不重跑 ASR，也不丢另一种语言已经翻好的成果
            restore_translations(checkpoint, effective_language, target_language, rows)
            atomic_write_text(source_srt, build_srt(rows, mode="source"),
                              encoding="utf-8-sig")
            meta = build_checkpoint(
                video_id=video_id,
                source_url=url,
                extractor=extractor,
                source_language=effective_language,
                target_language=target_language,
                rows=rows,
                asr_done=True,
                complete=False,
                duration=duration,
                requested_source_language=source_language,
                translations=(checkpoint or {}).get("translations"),
            )
            save_checkpoint(checkpoint_path, meta)
            # ☠️ 只跑这次任务真正需要的阶段。source 模式要的就是原文那一份，
            # 上面已经写完了——再去调翻译服务，等于让"只要原文"被 Ollama 挂掉
            # 阻断，而它一行译文都不需要。summary 是另一回事：学习笔记要原文
            # 和译文成对，所以 source+summary 会尝试翻译，但翻译不可用时只
            # **跳过笔记**，不撤销已经完成的原文字幕。
            same_language = effective_language == target_language
            translation_failed = None
            if same_language:
                # 源和目标相同：没有可翻的东西，直接复用原文，别白跑模型
                print("源语言与目标语言相同，跳过翻译，仅导出原文。", flush=True)
                for row in rows:
                    row.pop("translation", None)
            elif subtitle_mode != "source":
                with timer.measure("翻译"):
                    translate_segments(
                        rows, effective_language, target_language,
                        checkpoint_path=checkpoint_path, checkpoint_meta=meta,
                    )
            elif summary:
                try:
                    with timer.measure("翻译"):
                        translate_segments(
                            rows, effective_language, target_language,
                            checkpoint_path=checkpoint_path, checkpoint_meta=meta,
                        )
                except OfflineSubtitleError as exc:
                    translation_failed = exc
                    print(f"翻译不可用，跳过学习笔记（原文字幕已完成）：{exc}",
                          file=sys.stderr, flush=True)
            else:
                print("只要原文，跳过翻译。", flush=True)
            # 有译文才导出译文/双语那两份；没有就不写，也不在返回值里
            # 报告不存在的成果
            has_translation = any(
                (row.get("translation") or "").strip() for row in rows)
            if has_translation:
                atomic_write_text(bilingual_srt, build_srt(rows, mode="bilingual"),
                                  encoding="utf-8-sig")
                atomic_write_text(target_srt, build_srt(rows, mode="target"),
                                  encoding="utf-8-sig")
            else:
                bilingual_srt = target_srt = None
            meta = build_checkpoint(
                video_id=video_id,
                source_url=url,
                extractor=extractor,
                source_language=effective_language,
                target_language=target_language,
                rows=rows,
                asr_done=True,
                complete=True,
                duration=duration,
                requested_source_language=source_language,
                translations=(checkpoint or {}).get("translations"),
            )
            save_checkpoint(checkpoint_path, meta)
            if summary and translation_failed is None:
                assert guide is not None
                try:
                    with timer.measure("学习笔记"):
                        write_learning_guide(rows, media.title, url, duration, effective_language, target_language, guide)
                except Exception as exc:
                    print(f"学习总结生成失败（字幕已完成）：{exc}", file=sys.stderr, flush=True)
                    guide = None
            else:
                guide = None
            # 用户选的那一份没能生成（source 之外都要译文）时，退回原文那一份，
            # 并说清楚——不要在返回值里指向一个不存在的文件
            chosen = {"source": source_srt, "target": target_srt,
                      "bilingual": bilingual_srt}[subtitle_mode] or source_srt
            print(timer.summary(duration), flush=True)
            print(f"完成：{chosen}", flush=True)
            return {
                "video": video,
                "media": media,
                "source_srt": source_srt,
                "target_srt": target_srt,
                "bilingual_srt": bilingual_srt,
                "subtitle": chosen,
                "subtitle_mode": subtitle_mode,
                "guide": guide,
                "requested_source_language": source_language,
                "detected_source_language": detected_language,
                "source_language": effective_language,
                "target_language": target_language,
                "segments": len(rows),
                "duration": duration,
                "stage_seconds": dict(timer.stages),
                "stages_reused": list(timer.skipped),
            }
        finally:
            audio.unlink(missing_ok=True)


def process_input(
    source: str | Path,
    output_dir: str | Path | None = None,
    source_language: str = "auto",
    max_height: int = 2160,
    summary: bool = True,
    target_language: str | None = None,
    subtitle_mode: str | None = None,
) -> dict[str, Path | str | int | None]:
    """一个入口收两种输入：网络地址或本地文件。= resolve_media 之后 process_media。

    ☠️ 顺序是有意的，四步一步都不能换：
    1. 冻结选项——非法参数必须在**任何网络访问之前**失败；
    2. 识别输入、算出任务目录——只读，不下载；
    3. 上任务锁；
    4. 在锁内下载 + 处理。下载在锁外做的话，两个进程会同时往同一个任务目录
       里写半个文件（这条是原设计就有的，别在重构里弄丢）。
    """
    options = build_task_options(
        source_language=source_language,
        target_language=target_language,
        subtitle_mode=subtitle_mode,
        summary=summary,
        max_height=max_height,
    )
    source = resolve_share_input(source)
    root = Path(output_dir) if output_dir else Path(repo_path("downloads"))
    plan = plan_media(source, root, options.max_height)
    with JobLock(plan.job_dir / "_job.lock"):
        media = plan.fetch()
        # 兜底不变量：process_media 会按 media 算目录，而这里握着的是按 plan
        # 算出来的那把锁。两者必须是同一个目录，否则就是"持 A 的锁写 B"
        if (root / media.job_key).resolve() != plan.job_dir.resolve():
            raise OfflineSubtitleError(
                f"任务目录与已上锁的目录不一致（{plan.job_dir.name} ≠ "
                f"{media.job_key}），已停止。请重新运行一次。")
        return process_media(media, options, root, lock_held=True)


def process_url(
    url: str,
    output_dir: str | Path | None = None,
    source_language: str = "auto",
    max_height: int = 2160,
    summary: bool = True,
    target_language: str | None = None,
    subtitle_mode: str | None = None,
) -> dict[str, Path | str | int | None]:
    """旧入口，保留兼容：等价于 process_input（它现在也吃本地路径）。"""
    return process_input(
        url, output_dir, source_language, max_height, summary,
        target_language=target_language, subtitle_mode=subtitle_mode,
    )


def process_local_file(
    path: str | Path,
    output_dir: str | Path | None = None,
    **kwargs,
) -> dict[str, Path | str | int | None]:
    """本地视频/音频导入。走和网络任务完全相同的处理链，只是不下载。"""
    if looks_like_url(path):
        raise OfflineSubtitleError(f"这是一个网络地址，不是本地文件：{path}")
    return process_input(path, output_dir, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载或导入视频并生成本地字幕（原文 / 译文 / 双语 SRT）。")
    parser.add_argument("url", metavar="来源", help="视频地址，或本地视频/音频文件路径")
    parser.add_argument("--output-dir", type=Path, help="任务根目录，默认仓库 downloads/")
    parser.add_argument("--source-language", default="auto", help="Whisper 语言代码，或 auto")
    parser.add_argument(
        "--target-language", default=None,
        help="译文语言，如 en / de。不给时：中文视频默认 en，其他默认 zh")
    parser.add_argument(
        "--subtitle-mode", default=DEFAULT_SUBTITLE_MODE, choices=list(SUBTITLE_MODES),
        help="bilingual=原文+译文（默认）、target=只要译文、source=只要原文")
    parser.add_argument("--max-height", type=int, default=2160,
                        help="最高画面高度，默认 2160（本地文件忽略此项）")
    parser.add_argument("--no-summary", action="store_true", help="不生成学习笔记")
    parser.add_argument(
        "--list-urls", action="store_true",
        help="只从这段分享文案里抠出链接，一行一个，然后退出（给 PowerShell 调用）")
    args = parser.parse_args(argv)
    if args.list_urls:
        # 抠链接的规则只有这一份，PowerShell 那边调过来，别再各写一套正则
        for url in extract_share_urls(args.url):
            print(url)
        return 0
    try:
        process_input(
            args.url, args.output_dir, args.source_language, args.max_height,
            not args.no_summary,
            target_language=args.target_language,
            subtitle_mode=args.subtitle_mode,
        )
    except OfflineSubtitleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
