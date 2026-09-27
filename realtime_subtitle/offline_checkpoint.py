"""离线任务的缓存身份与落盘：checkpoint schema、指纹、多目标翻译表、原子写。

从 offline.py 拆出来的（2026-09-27）。CLAUDE.md 第 4 节第 37 条讲的全在这里：
ASR 指纹按"用户请求的模式"算、schema v2 作废 v1、坏缓存作废而不是夹紧修好、
翻译按 (source,target) 分表存。这一块只依赖标准库和 config，不碰网站、
不碰模型、不碰线程——所以能单独拆出来，也最适合单独读。

☠️ offline.py 把这里的名字**全部 re-export**。测试一直按 `offline.X` 拿，
`offline.save_checkpoint` 还会被测试打桩——打桩只对 offline 命名空间里的
调用生效，所以**调用 save_checkpoint 的代码（translate_segments /
process_media）必须留在 offline.py**，别跟着挪过来。本模块内部也不许调用
save_checkpoint，否则那个桩会悄悄失效。

为什么 offline.py 其余部分没有按"导入层 / 处理层"拆：测试在约 90 处按
`offline.X` 打桩（_load_yt_dlp、download_video、_extract_audio、
transcribe_audio、_ollama_request……），而调用它们的正是 plan_media /
process_media。把调用方挪进子模块，这些桩就打不到了；其中"桩成炸弹、断言
没被调用"的那类用例还会**静默变空**而不是变红。要拆那两层，得先把测试
改成对子模块打桩，并逐条确认每个桩仍然生效。
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

from realtime_subtitle import config


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
