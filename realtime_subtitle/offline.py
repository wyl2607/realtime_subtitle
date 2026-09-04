"""Download media, transcribe it locally, and export bilingual subtitles."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from realtime_subtitle import config
from realtime_subtitle.paths import repo_path


_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
_REFUSAL_MARKERS = ("无法完成", "法律法规", "政治敏感", "不能生成", "其他非敏感")


class OfflineSubtitleError(RuntimeError):
    """A user-actionable failure in the batch download/subtitle pipeline."""


def target_language_for(source_language: str) -> str:
    """Chinese source gets German; every other source gets Chinese."""
    return "de" if (source_language or "").lower().startswith("zh") else "zh"


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


def build_srt(rows: list[dict], bilingual: bool = True) -> str:
    """Render source-first, target-second SRT blocks."""
    blocks = []
    for index, row in enumerate(rows, 1):
        body = row["text"].strip()
        target = row.get("translation", "").strip()
        if bilingual and target:
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


def _load_yt_dlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise OfflineSubtitleError(
            "缺少 yt-dlp。请运行 venv\\Scripts\\pip install -r requirements.txt 后重试。"
        ) from exc
    return yt_dlp


def _video_id(info: dict, url: str) -> str:
    raw = str(info.get("id") or "").strip()
    if not raw:
        raw = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def download_video(url: str, job_dir: Path, max_height: int = 2160) -> tuple[dict, Path]:
    """Download the best available video up to max_height plus audio."""
    if max_height < 1:
        raise OfflineSubtitleError("最高画质高度必须是正整数。")
    if not shutil.which("ffmpeg"):
        raise OfflineSubtitleError("找不到 ffmpeg。请安装 ffmpeg 并加入 PATH。")
    yt_dlp = _load_yt_dlp()
    job_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_video(job_dir, job_dir.name)
    if existing:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise OfflineSubtitleError(f"无法读取已下载视频的信息：{exc}") from exc
        return info, existing

    output = str(job_dir / f"{job_dir.name}.%(ext)s")
    options = {
        "format": f"bv*[height<={max_height}]+ba/b",
        "outtmpl": output,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise OfflineSubtitleError(f"视频下载失败：{exc}") from exc

    video = _find_video(job_dir, job_dir.name)
    if not video:
        video_id = _video_id(info, url)
        video = _find_video(job_dir, video_id)
    if not video:
        raise OfflineSubtitleError("视频下载完成，但没有找到合并后的视频文件。请检查 yt-dlp/ffmpeg。")
    return info, video


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


def _translation_prompt(source_language: str, target_language: str, text: str) -> str:
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
    return f"""你是{source_name}{role}。请把下面这一条字幕翻译成自然、准确的{target_name}。

要求：
{rules}
只翻译当前这一条，不要补充、解释、拒答，也不要输出{source_name}原文。
即使涉及政治、战争、政党或其他敏感话题，也只做语言翻译。
当前条目若是半句，只翻这半句，不要擅自补全。数字、人名、机构名和地名不得改写。{glossary}

{source_name}原文：
{text}

{target_name}译文："""


def _clean_translation(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I).strip()
    text = re.sub(r"^(?:简体中文翻译|中文翻译|翻译|简体中文|德语翻译)\s*[:：]\s*", "", text)
    return text.strip().strip('"“”')


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
    return _clean_translation(response.json().get("response", ""))


def translate_segments(rows: list[dict], source_language: str, target_language: str) -> None:
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
    session = requests.Session()
    for index, row in enumerate(rows, 1):
        prompt = _translation_prompt(source_language, target_language, row["text"])
        result = ""
        last_error = None
        for model in models:
            if not model:
                continue
            for attempt in range(2):
                try:
                    result = _ollama_request(session, ollama_url, model, prompt)
                    if result and not any(marker in result for marker in _REFUSAL_MARKERS):
                        break
                except Exception as exc:
                    last_error = exc
                time.sleep(1.0)
            if result and not any(marker in result for marker in _REFUSAL_MARKERS):
                break
        if not result or any(marker in result for marker in _REFUSAL_MARKERS):
            raise OfflineSubtitleError(f"第 {index} 条字幕翻译失败：{last_error or '本地模型没有返回译文'}")
        row["translation"] = result
        if index % 20 == 0 or index == 1:
            print(f"翻译进度：{index}/{len(rows)}", flush=True)


def _summary_chunk_prompt(rows: list[dict]) -> str:
    lines = "\n".join(
        f"[{_srt_time(row['start'])}] {row['text']} → {row.get('translation', '')}"
        for row in rows
    )
    return f"""请把下面一段视频字幕压缩成 2—3 条中文要点，每条不超过 50 字。
只根据字幕内容，区分主持人/说话人的观点，不补充外部事实，不评价，不输出原文。

{lines}
"""


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
    session = requests.Session()
    summaries = []
    for start in range(0, len(rows), 40):
        summaries.append(_ollama_request(session, ollama_url, config.OLLAMA_MODEL, _summary_chunk_prompt(rows[start:start + 40]), 320))
    summary_text = "\n".join(f"片段 {i + 1}：{text[:280]}" for i, text in enumerate(summaries))
    candidates = []
    for row in rows:
        source = row["text"].strip()
        target = row.get("translation", "").strip()
        if 20 <= len(source) <= 100 and source:
            candidates.append(f"{source} → {target}")
    candidates = candidates[::max(1, len(candidates) // 24)][:24]
    final_prompt = f"""请根据下面的分段摘要和表达候选，为中文学习者写一份德语/外语视频学习笔记。

必须输出 Markdown，并且完整包含：
## 内容概述（一段）
## 对话脉络（按顺序 5—7 条）
## 重点词汇与表达（选 10—15 条，格式：原文 — 中文含义；学习提示）
## 学习方法（3 步，说明如何配合双语 SRT）

只整理视频字幕里出现的内容，不补充外部事实；政治内容只写“视频中表示/主持人认为”等，不做事实核查。

【分段摘要】
{summary_text}

【表达候选】
{chr(10).join('- ' + item for item in candidates)}
"""
    guide = _ollama_request(session, ollama_url, config.OLLAMA_MODEL, final_prompt, 1800)
    minutes, seconds = divmod(int(duration), 60)
    header = f"""# 字幕学习笔记

- **视频**：{title}
- **来源**：{url}
- **时长**：约 {minutes}:{seconds:02d}
- **源语言**：{_language_name(source_language)}
- **目标语言**：{_language_name(target_language)}
- **字幕规则**：每条先显示原文，再显示翻译；中文源语言翻译为德语，其他源语言翻译为中文

> 本文件只整理视频字幕内容，不是事实核查；个别自动识别的专名或半句请结合原音确认。

"""
    output.write_text(header + guide.strip() + "\n", encoding="utf-8")


def process_url(
    url: str,
    output_dir: str | Path | None = None,
    source_language: str = "auto",
    max_height: int = 2160,
    summary: bool = True,
) -> dict[str, Path | str | int | None]:
    """Run download → ASR → translation → SRT → optional study guide."""
    source_language = (source_language or "auto").strip().lower()
    root = Path(output_dir) if output_dir else Path(repo_path("downloads"))
    root.mkdir(parents=True, exist_ok=True)
    yt_dlp = _load_yt_dlp()
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise OfflineSubtitleError(f"无法读取视频信息：{exc}") from exc
    video_id = _video_id(info, url)
    job_dir = root / video_id
    video_info, video = download_video(url, job_dir, max_height)
    job_dir.mkdir(parents=True, exist_ok=True)
    audio = job_dir / "_audio_16k.wav"
    source_srt = job_dir / f"{video_id}_source.srt"
    bilingual_srt = job_dir / f"{video_id}_bilingual.srt"
    guide: Path | None = job_dir / f"{video_id}_learning_guide.md" if summary else None
    try:
        print(f"开始处理：{video_info.get('title', video_id)}", flush=True)
        _extract_audio(video, audio)
        rows, detected_language, duration = transcribe_audio(audio, source_language)
        if not rows:
            raise OfflineSubtitleError("没有识别到可用语音。")
        effective_language = source_language if source_language != "auto" else detected_language
        source_srt.write_text(build_srt(rows, bilingual=False), encoding="utf-8-sig")
        target_language = target_language_for(effective_language)
        translate_segments(rows, effective_language, target_language)
        bilingual_srt.write_text(build_srt(rows, bilingual=True), encoding="utf-8-sig")
        if summary:
            assert guide is not None
            try:
                write_learning_guide(rows, str(video_info.get("title", video_id)), url, duration, effective_language, target_language, guide)
            except Exception as exc:
                print(f"学习总结生成失败（字幕已完成）：{exc}", file=sys.stderr, flush=True)
                guide = None
        print(f"完成：{bilingual_srt}", flush=True)
        return {
            "video": video,
            "source_srt": source_srt,
            "bilingual_srt": bilingual_srt,
            "guide": guide,
            "source_language": effective_language,
            "target_language": target_language,
            "segments": len(rows),
        }
    finally:
        audio.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download media and create source+translation SRT subtitles.")
    parser.add_argument("url", help="video URL")
    parser.add_argument("--output-dir", type=Path, help="download root; defaults to the repository downloads/")
    parser.add_argument("--source-language", default="auto", help="Whisper language code or auto")
    parser.add_argument("--max-height", type=int, default=2160, help="maximum video height, default 2160")
    parser.add_argument("--no-summary", action="store_true", help="skip the local-model learning guide")
    args = parser.parse_args(argv)
    try:
        process_url(args.url, args.output_dir, args.source_language, args.max_height, not args.no_summary)
    except OfflineSubtitleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
