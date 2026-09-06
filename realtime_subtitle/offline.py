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
from pathlib import Path

import requests

from realtime_subtitle import config
from realtime_subtitle.paths import repo_path


_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
_REFUSAL_MARKERS = ("无法完成", "法律法规", "政治敏感", "不能生成", "其他非敏感")


class OfflineSubtitleError(RuntimeError):
    """A user-actionable failure in the batch download/subtitle pipeline."""


class TruncatedModelOutput(OfflineSubtitleError):
    """The model hit its output limit before finishing."""


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


def download_video(url: str, job_dir: Path, max_height: int = 2160) -> tuple[dict, Path]:
    """Download the best available video up to max_height plus audio."""
    if max_height < 1:
        raise OfflineSubtitleError("最高画质高度必须是正整数。")
    if not shutil.which("ffmpeg"):
        raise OfflineSubtitleError("找不到 ffmpeg。请安装 ffmpeg 并加入 PATH。")
    yt_dlp = _load_yt_dlp()
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise OfflineSubtitleError(f"无法读取视频信息：{exc}") from exc
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

    extractor = _normalize_extractor(info)
    video_id = _video_id(info, url)
    video = _find_video(job_dir, job_dir.name)
    if not video:
        video = _find_video(job_dir, video_id)
    if not video:
        raise OfflineSubtitleError("视频下载完成，但没有找到合并后的视频文件。请检查 yt-dlp/ffmpeg。")
    save_media_source(job_dir, extractor, video_id, url)
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


CHECKPOINT_VERSION = 1
TX_STRATEGY_VERSION = 2


def _fingerprint_payload(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def asr_fingerprint(source_language: str) -> str:
    seed = (getattr(config, "LANGUAGE_SEED_PROMPTS", {}) or {}).get(source_language, "")
    return _fingerprint_payload({
        "source_language": source_language or "",
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
) -> dict:
    return {
        "version": CHECKPOINT_VERSION,
        "video_id": video_id,
        "source_url": source_url,
        "extractor": extractor or "",
        "source_language": source_language,
        "target_language": target_language,
        "asr_fingerprint": asr_fingerprint(source_language),
        "tx_fingerprint": tx_fingerprint(source_language, target_language),
        "rows": rows,
        "duration": duration,
        "asr_done": asr_done,
        "complete": complete,
    }


def save_checkpoint(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
        return None
    if not isinstance(data.get("rows"), list):
        return None
    for row in data["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            return None
        try:
            start = float(row["start"])
            end = float(row["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(end):
            return None
        if "translation" in row and not isinstance(row["translation"], str):
            return None
    return data


def checkpoint_asr_usable(
    data: dict | None,
    video_id: str,
    source_language: str,
    source_url: str | None = None,
    extractor: str | None = None,
) -> bool:
    if not data or not data.get("asr_done") or not data.get("rows"):
        return False
    if data.get("video_id") != video_id:
        return False
    if source_url is not None and data.get("source_url") != source_url:
        return False
    if extractor is not None and data.get("extractor") != extractor:
        return False
    if data.get("source_language") != source_language:
        return False
    return data.get("asr_fingerprint") == asr_fingerprint(source_language)


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
        if not checkpoint_path:
            return
        payload = dict(checkpoint_meta or {})
        payload["version"] = payload.get("version", CHECKPOINT_VERSION)
        payload["rows"] = rows
        payload["asr_done"] = True
        payload["complete"] = False
        save_checkpoint(checkpoint_path, payload)

    with requests.Session() as session:
        for index, row in enumerate(rows, 1):
            if (row.get("translation") or "").strip():
                continue
            prompt = _translation_prompt(source_language, target_language, row["text"])
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
                        if result and not any(marker in result for marker in _REFUSAL_MARKERS):
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
                if result and not any(marker in result for marker in _REFUSAL_MARKERS):
                    break
            if not result or any(marker in result for marker in _REFUSAL_MARKERS):
                persist()
                raise OfflineSubtitleError(f"第 {index} 条字幕翻译失败：{last_error or '本地模型没有返回译文'}")
            row["translation"] = result
            persist()
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


def _vocab_requirement(n: int) -> str:
    if n <= 0:
        return "## 重点词汇与表达（本次无合适候选，不要编造词条）"
    shown = min(15, n)
    return f"## 重点词汇与表达（选 {shown} 条，格式：原文 — 中文含义；学习提示）"


def _final_guide_prompt(summary_text: str, candidate_block: str, n_candidates: int) -> str:
    return f"""请根据下面的分段摘要和表达候选，为中文学习者写一份德语/外语视频学习笔记。

必须输出 Markdown，并且完整包含：
## 内容概述（一段）
## 对话脉络（按顺序 5—7 条）
{_vocab_requirement(n_candidates)}
## 学习方法（3 步，说明如何配合双语 SRT）

只整理视频字幕里出现的内容，不补充外部事实；政治内容只写“视频中表示/主持人认为”等，不做事实核查。

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


def _guide_has_required_sections(text: str) -> bool:
    body = text or ""
    return all(section in body for section in _GUIDE_SECTIONS)


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
    candidates = _fit_lines_to_budget(_candidate_lines(rows), leftover)
    candidate_block = "\n".join(candidates)
    probe = _final_guide_prompt("摘要", candidate_block, len(candidates))
    while candidates and len(probe) > final_budget:
        candidates = candidates[:-1]
        candidate_block = "\n".join(candidates)
        probe = _final_guide_prompt("摘要", candidate_block, len(candidates))
    if len(probe) > final_budget:
        raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")

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
            final_prompt = _final_guide_prompt(
                summary_text, candidate_block, len(candidates))
            if len(final_prompt) <= final_budget:
                break
            if candidates:
                drop = max(1, len(candidates) // 4)
                candidates = candidates[:-drop]
                candidate_block = "\n".join(candidates)
                continue
            if len(summaries) == 1:
                raise OfflineSubtitleError("学习笔记最终输入超出上下文预算。")
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
- **字幕规则**：每条先显示原文，再显示翻译；中文源语言翻译为德语，其他源语言翻译为中文

> 本文件只整理视频字幕内容，不是事实核查；个别自动识别的专名或半句请结合原音确认。

"""
    atomic_write_text(output, header + guide.strip() + "\n", encoding="utf-8")


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
    extractor = _normalize_extractor(info)
    job_dir = root / job_key(extractor, video_id)
    audio = job_dir / "_audio_16k.wav"
    source_srt = job_dir / f"{video_id}_source.srt"
    bilingual_srt = job_dir / f"{video_id}_bilingual.srt"
    checkpoint_path = job_dir / "_job_checkpoint.json"
    guide: Path | None = job_dir / f"{video_id}_learning_guide.md" if summary else None
    with JobLock(job_dir / "_job.lock"):
        try:
            video_info, video = download_video(url, job_dir, max_height)
            extractor = _normalize_extractor(video_info) or extractor
            print(f"开始处理：{video_info.get('title', video_id)}", flush=True)
            checkpoint = load_checkpoint(checkpoint_path)
            locked_source = source_language if source_language != "auto" else None
            rows = []
            duration = 0.0
            effective_language = locked_source or "und"
            checkpoint_source = checkpoint.get("source_language") if checkpoint else None
            resume_source = locked_source or checkpoint_source
            if resume_source and checkpoint_asr_usable(
                checkpoint, video_id, resume_source, url, extractor,
            ):
                rows = list(checkpoint["rows"])
                duration = float(checkpoint.get("duration") or 0.0)
                effective_language = resume_source
            else:
                _extract_audio(video, audio)
                rows, detected_language, duration = transcribe_audio(audio, source_language)
                if not rows:
                    raise OfflineSubtitleError("没有识别到可用语音。")
                effective_language = source_language if source_language != "auto" else detected_language
            target_language = target_language_for(effective_language)
            if checkpoint and not checkpoint_tx_usable(
                checkpoint, effective_language, target_language,
                video_id, url, extractor,
            ):
                for row in rows:
                    row.pop("translation", None)
            atomic_write_text(source_srt, build_srt(rows, bilingual=False), encoding="utf-8-sig")
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
            )
            save_checkpoint(checkpoint_path, meta)
            translate_segments(
                rows, effective_language, target_language,
                checkpoint_path=checkpoint_path, checkpoint_meta=meta,
            )
            atomic_write_text(bilingual_srt, build_srt(rows, bilingual=True), encoding="utf-8-sig")
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
            )
            save_checkpoint(checkpoint_path, meta)
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
    parser = argparse.ArgumentParser(description="下载视频并生成本地双语字幕（原文 + 译文 SRT）。")
    parser.add_argument("url", help="视频地址")
    parser.add_argument("--output-dir", type=Path, help="下载根目录，默认仓库 downloads/")
    parser.add_argument("--source-language", default="auto", help="Whisper 语言代码，或 auto")
    parser.add_argument("--max-height", type=int, default=2160, help="最高画面高度，默认 2160")
    parser.add_argument("--no-summary", action="store_true", help="不生成学习笔记")
    args = parser.parse_args(argv)
    try:
        process_url(args.url, args.output_dir, args.source_language, args.max_height, not args.no_summary)
    except OfflineSubtitleError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
