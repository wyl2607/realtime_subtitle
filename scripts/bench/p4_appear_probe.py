#!/usr/bin/env python3
"""P4「出现体验」UI 侧基线探针（只测量，不改产品代码）。

在 QT_QPA_PLATFORM=offscreen 下构造真实 SubtitleWindow，用虚拟时钟直接调用
字幕槽函数（不 sleep），按确定性时间线重放 live 词流 → 草稿 → 正式句对 →
偶发 update_dst 原位替换，统计：

  * 每句驻留时长（进入可见集合 → 被挤出可见集合）
  * 可见句对集合/顺序变化次数、QLabel 文本实际变化次数、旧句 y 跳动
  * 草稿/句对事件到达 → QLabel 文本更新（UI 侧同步耗时，对照应 ≈ 0）

用法：
  QT_QPA_PLATFORM=offscreen python scripts/bench/p4_appear_probe.py --pairs-fit 2
  QT_QPA_PLATFORM=offscreen python scripts/bench/p4_appear_probe.py --pairs-fit 6 --json out.json
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:  # 项目惯例：torch 先于 Qt 导入（Windows 上 ctranslate2/torch 的 DLL 顺序保险）
    import torch  # noqa: F401
except ImportError:
    pass

from PyQt6.QtWidgets import QApplication  # noqa: E402

_APP = None  # 模块级持有：QApplication 被 GC 后建 QWidget 会 qFatal 秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


# 自写合成句子（不来自 transcripts/ 或任何用户数据）。
SENTENCES = [
    ("Heute sprechen wir über das Wetter in Norddeutschland.", "今天我们聊聊德国北部的天气。"),
    ("Am Morgen ist der Himmel noch fast vollständig bedeckt.", "清晨天空还几乎完全被云遮住。"),
    ("Gegen Mittag soll die Sonne endlich wieder herauskommen.", "接近中午阳光应该会重新露出来。"),
    ("Die Temperaturen liegen zwischen acht und vierzehn Grad.", "气温大约在八度到十四度之间。"),
    ("Für die Küste warnen die Behörden vor starkem Westwind.", "沿海地区官方提醒注意强劲的西风。"),
    ("Im Binnenland bleibt es dagegen auffallend ruhig und trocken.", "内陆地区则保持异常平静干燥。"),
    ("Autofahrer sollten am Abend mit glatten Straßen rechnen.", "司机晚上应留意路面可能结冰。"),
    ("Die Bahn meldet kleinere Verspätungen im Regionalverkehr.", "铁路方面报告区域交通有轻微延误。"),
    ("Wer mit dem Fahrrad unterwegs ist, braucht heute gute Beleuchtung.", "今天骑车出行的人需要良好的车灯。"),
    ("Morgen wird es dann langsam wieder etwas wärmer.", "明天开始天气会慢慢回暖一些。"),
    ("Zum Wochenende erwarten die Meteorologen erste Frühlingsboten.", "周末气象学家预计会出现初春的迹象。"),
    ("Wir bleiben für Sie am Ball und melden jede neue Entwicklung.", "我们会持续关注并报道每一条新进展。"),
]
# 间歇性 update_dst 的内容（更晚到达的润色译文，长度与原来不同）。
UPDATED = {
    1: "更准确地说，清晨天空还几乎完全被云层遮住，只有东边偶尔透出一点微弱的亮光，出门的人最好还是带上一把伞以防万一。",
    4: "沿海地区官方提醒：今天下午起会有强劲西风，阵风可能达到八级，请远离岸边并固定好阳台上的物品。",
    7: "铁路方面报告，区域交通出现了轻微延误，主要影响北部几条线路，请乘客留意站内广播和手机通知。",
    10: "周末气象学家预计，初春的迹象会开始出现，白天的气温也会比本周明显回升一些，适合出门散步。",
}
GAPS = [2.5, 3.0, 3.5, 4.0, 5.0, 2.2, 2.8, 3.2, 4.5, 2.4, 3.8, 3.0]
STREAM_SEC = 2.0
UPDATE_DELAY = 0.8
DRAFT_STEP = 0.15


def _pct(values, p):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = round((len(ordered) - 1) * p / 100.0)
    return ordered[max(0, min(len(ordered) - 1, index))]


def _r(value, digits=3):
    return None if value is None else round(value, digits)


class _VirtualTimer:
    """替换真 QTimer：探针用虚拟时钟手动检查 _hold_deadline。

    否则真定时器会在虚拟时间还没到、但墙钟已经过去若干毫秒时提前触发，
    把 minHold 的数字污染成「取决于探针跑多快」。
    """

    def __init__(self):
        self._active = False

    def start(self, _ms):
        self._active = True

    def stop(self):
        self._active = False

    def isActive(self):
        return self._active


def _item_key(win, item):
    fn = getattr(win, "_pair_key", None)
    if fn is not None:
        return fn(item)
    return item[6] if len(item) >= 7 else None


def _visible_pair_items(win):
    """按 _render 的同一套规则，返回候选句对 [(key, german, chinese)]。"""
    import realtime_subtitle.config as config
    keep = max(1, int(getattr(config, "MAX_SENTENCE_PAIRS", 20)))
    items = []
    for item in reversed(win.sentence_pairs):
        g, c = item[0], item[1]
        k = _item_key(win, item)
        if not win._pair_html(g, c, key=k):
            continue
        items.append((k, g, c))
        if len(items) >= keep:
            break
    items.reverse()
    return items


def _fixed_blocks(win):
    import html as _html
    fixed = []
    live = win._live_block_html()
    if live:
        fixed.append(live)
    if win.status_line:
        fixed.append(_html.escape(win.status_line))
    return fixed


def _layout_snapshot(win):
    """返回 (visible_keys, y_from_bottom_by_key, doc_total_height)。

    y_from_bottom = 文档总高 - 该句块之前所有句块的高度，即 QLabel 底对齐时
    该句块顶部离窗口底边的距离。live/status 在句对下方，它们变高会把旧句顶
    上去——这正是要量到的「整窗重排」。

    可见集合优先直接读 win._visible_pair_keys（改后产品代码的唯一真相源）；
    没有这个属性时（改前基线代码）才按 _render 的二分复刻一遍。
    """
    items = _visible_pair_items(win)
    fixed = _fixed_blocks(win)
    if not items and not fixed:
        return [], {}, 0.0

    doc = win._doc_for_render()
    avail_h = win.window.height() - 40

    sticky_keys = getattr(win, "_visible_pair_keys", None)
    if sticky_keys is not None:
        item_map = {k: (k, g, c) for k, g, c in items}
        selected = [item_map[k] for k in sticky_keys if k in item_map]
    else:
        cap = len(items)

        def blocks_for(count):
            chosen = items[len(items) - count:]
            return [
                win._pair_html(g, c, dim=(idx < len(chosen) - 1), top_gap=(idx > 0), key=k)
                for idx, (k, g, c) in enumerate(chosen)
            ]

        def fits(count):
            doc.setHtml(win._join_render_blocks(blocks_for(count), fixed))
            return doc.size().height() <= avail_h

        shown = 0
        if cap:
            shown = 1
            lo, hi = 2, cap
            while lo <= hi:
                mid = (lo + hi) // 2
                if fits(mid):
                    shown = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
        selected = items[len(items) - shown:] if shown else []

    blocks = [
        win._pair_html(g, c, dim=(idx < len(selected) - 1),
                       top_gap=(idx > 0), key=k)
        for idx, (k, g, c) in enumerate(selected)
    ]

    heights = [0.0]
    for count in range(1, len(blocks) + 1):
        doc.setHtml(win._join_render_blocks(blocks[:count], []))
        heights.append(doc.size().height())

    doc.setHtml(win._join_render_blocks(blocks, fixed))
    total = doc.size().height()

    keys = [k for k, _, _ in selected]
    y_by_key = {}
    for index, key in enumerate(keys):
        y_by_key[key] = total - heights[index]
    return keys, y_by_key, total


def _make_timeline():
    """返回按 (虚拟秒, 顺序) 排好的事件列表。"""
    events = []
    t = 0.5
    for index, (german, chinese) in enumerate(SENTENCES):
        key = f"s{index:02d}"
        words = german.split()
        for step in (1, 2, 3):
            at = t + step * 0.5
            committed = " ".join(words[: max(1, len(words) * step // 4)])
            unstable = " ".join(words[max(1, len(words) * step // 4):])
            events.append((at, 1, "live", (committed, unstable)))
        at = t + DRAFT_STEP
        while at < t + STREAM_SEC - 1e-9:
            keep = max(2, round(len(chinese) * at / STREAM_SEC))
            events.append((at, 2, "draft", chinese[:keep]))
            at += DRAFT_STEP
        events.append((t + STREAM_SEC, 3, "live_clear", ("", "")))
        events.append((t + STREAM_SEC, 4, "pair", (german, chinese, key)))
        if index in UPDATED:
            events.append((t + STREAM_SEC + UPDATE_DELAY, 5, "update_dst", (key, UPDATED[index])))
        t += GAPS[index % len(GAPS)]
    events.sort(key=lambda item: (item[0], item[1]))
    return events, t


def _calibrate_height(win, pairs_fit):
    """按「典型 live 行 + pairs_fit 句历史」定窗口高度。

    句对都是同一字号、宽度 1200 下各占一行，高度一致。用一段说了一半的 live
    行（committed + unstable + 草稿）作为典型占用，这样说话过程中刚好能看到
    pairs_fit 句；句间 live 清空时可能多出 1 句（产品里本来就随 live 高度浮动）。
    """
    sample_de, sample_zh = SENTENCES[7]
    cut = len(sample_de) * 3 // 5
    saved = (win.live_committed, win.live_unstable, win.live_draft)
    try:
        win.live_committed = sample_de[:cut]
        win.live_unstable = sample_de[cut:]
        win.live_draft = sample_zh
        live_html = win._live_block_html()
    finally:
        win.live_committed, win.live_unstable, win.live_draft = saved

    blocks = [
        win._pair_html(german, chinese, dim=(index < pairs_fit - 1),
                       top_gap=(index > 0), key=f"fit{index}")
        for index, (german, chinese) in enumerate(SENTENCES[:pairs_fit])
    ]
    doc = win._doc_for_render()
    doc.setHtml(win._join_render_blocks(blocks, [live_html]))
    return max(140, int(round(doc.size().height() + 40 + 2)))


def _suppress_construct(func):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return func()


def run(pairs_fit):
    _app()
    import realtime_subtitle.config as config
    import realtime_subtitle.ui.design_tokens as design_tokens
    import realtime_subtitle.ui.subtitle_window as subtitle_window

    config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
    config.MAX_SENTENCE_PAIRS = 20

    original_load = subtitle_window.SubtitleWindow._load_state
    subtitle_window.SubtitleWindow._load_state = staticmethod(lambda: {})
    try:
        win = _suppress_construct(subtitle_window.SubtitleWindow)
    finally:
        subtitle_window.SubtitleWindow._load_state = original_load

    win._state_timer.stop()
    win._status_clear_timer.stop()
    win._save_state_if_changed = lambda: None

    width = 1200
    height = _calibrate_height(win, pairs_fit)
    win.container.setGeometry(0, 0, width, height)
    win._on_container_resize()
    _app().processEvents()

    # 预热一次完整的上屏路径（字体/文档布局/历史窗/电视窗首次调用），
    # 否则首句 pair 会把一次性 font-cache 开销算进「首字延迟」。
    win._add_pair("Warm-up sentence for layout cache.", "用于预热排版的句子。",
                  {"revision": 0, "t0": None, "t1": None, "key": "warmup", "source": "local"})
    win.sentence_pairs.clear()
    win.live_draft = ""
    win._render()
    _app().processEvents()

    events, end_t = _make_timeline()
    min_hold = float(design_tokens.MOTION_MIN_HOLD_SEC)

    # 改后代码有 _now：换成虚拟时钟，并用手动 tick 检查 _hold_deadline。
    # 改前基线没有这些接缝，保持原样跑（同一脚本两种代码都能跑）。
    virtual = {"t": 0.0}
    has_virtual_clock = hasattr(win, "_now")
    if has_virtual_clock:
        win._hold_timer.stop()
        win._hold_timer = _VirtualTimer()
        win._now = lambda: virtual["t"]
        win._visible_pair_keys = []
        win._shown_at.clear()
        if hasattr(win, "_ever_shown"):
            win._ever_shown.clear()
        win._hold_deadline = None
        if hasattr(win, "_stop_fade"):
            win._stop_fade()
        win._render_layout_sig = None
        win._render_lang_revision = None
        win._render()
        _app().processEvents()

    def release_if_due():
        deadline = getattr(win, "_hold_deadline", None)
        if deadline is not None and virtual["t"] + 1e-9 >= deadline:
            win._on_hold_release()

    def tick_to(target):
        while virtual["t"] + 1e-9 < target:
            virtual["t"] = round(min(target, virtual["t"] + 0.1), 6)
            release_if_due()

    first_seen = {}
    residence = {}
    visible_hist = {}
    settext_changes = 0
    visibility_changes = 0
    jump_events = 0
    jump_pairs = 0
    max_shift = 0.0
    by_kind = {}
    latencies = []
    latency_by_kind = {}
    ever_visible = set()
    reentries = 0
    reentry_keys = set()
    fade_events = 0
    old_key_refades = 0
    fade_prev = getattr(win, "_fade_key", None)

    def kind_stats(kind):
        return by_kind.setdefault(kind, {"events": 0, "settext_changes": 0,
                                         "visibility_changes": 0, "events_with_old_pair_y_jump": 0,
                                         "old_pair_y_jumps": 0, "max_old_pair_shift_px": 0.0})

    for at, _order, kind, payload in events:
        if has_virtual_clock:
            tick_to(at)
        stats = kind_stats(kind)
        stats["events"] += 1
        before_keys, before_y, _ = _layout_snapshot(win)
        before_text = win.window.text()

        wall0 = time.perf_counter()
        target_key = None
        if kind == "live":
            win._update_live(*payload)
        elif kind == "live_clear":
            win._update_live(*payload)
        elif kind == "draft":
            win._update_draft(payload)
        elif kind == "pair":
            german, chinese, key = payload
            target_key = key
            meta = {"revision": 0, "t0": None, "t1": None, "key": key, "source": "local"}
            win._add_pair(german, chinese, meta)
        elif kind == "update_dst":
            key, dst = payload
            target_key = key
            win.update_dst(key, dst)
        wall_ms = (time.perf_counter() - wall0) * 1000.0
        if has_virtual_clock:
            release_if_due()

        after_text = win.window.text()
        if after_text != before_text:
            settext_changes += 1
            stats["settext_changes"] += 1
            if kind in ("draft", "pair"):
                latencies.append(wall_ms)
                latency_by_kind.setdefault(kind, []).append(wall_ms)

        after_keys, after_y, _ = _layout_snapshot(win)
        visible_hist[len(after_keys)] = visible_hist.get(len(after_keys), 0) + 1

        # 淡入：记录 None → key 的转变；旧 key 曾经上过屏就不该再淡。
        # 探针不等 150ms 动画，直接推到终值（改前代码没有这些接缝）。
        cur_fade = getattr(win, "_fade_key", None)
        if cur_fade is not None and fade_prev is None:
            fade_events += 1
            if cur_fade in ever_visible:
                old_key_refades += 1
        if cur_fade is not None:
            finish_fade = getattr(win, "_finish_fade", None)
            if finish_fade is not None:
                finish_fade()
            else:
                anim = getattr(win, "_fade_anim", None)
                if anim is not None:
                    anim.setCurrentTime(anim.duration())
        fade_prev = getattr(win, "_fade_key", None)

        if after_keys != before_keys:
            visibility_changes += 1
            stats["visibility_changes"] += 1

        jumped = []
        for key in after_keys:
            if key in before_y and key in after_y:
                shift = abs(after_y[key] - before_y[key])
                if shift > 0.5:
                    jumped.append((key, shift))
        old_jumped = [item for item in jumped if item[0] != target_key]
        if old_jumped:
            jump_events += 1
            stats["events_with_old_pair_y_jump"] += 1
            jump_pairs += len(old_jumped)
            stats["old_pair_y_jumps"] += len(old_jumped)
            peak = max(shift for _, shift in old_jumped)
            max_shift = max(max_shift, peak)
            stats["max_old_pair_shift_px"] = max(stats["max_old_pair_shift_px"], peak)

        for key in after_keys:
            if key in ever_visible and key not in before_keys:
                reentries += 1
                reentry_keys.add(key)
            ever_visible.add(key)
            first_seen.setdefault(key, at)
        for key in list(first_seen):
            if key not in after_keys and key not in residence:
                residence[key] = at - first_seen[key]

    censored = [key for key in first_seen if key not in residence]
    censored_ages = [round(end_t - first_seen[key], 3) for key in censored]

    values = [round(v, 3) for v in residence.values()]
    under = sum(1 for v in residence.values() if v < min_hold)
    visible_counts = [int(k) for k, n in visible_hist.items() for _ in range(n)]

    result = {
        "config": {
            "pairs_fit": pairs_fit,
            "window_width": width,
            "window_height": height,
            "font_size": config.FONT_SIZE,
            "sentences": len(SENTENCES),
            "virtual_duration_sec": round(end_t - 0.5, 3),
            "min_hold_sec": min_hold,
            "fade_in_ms": design_tokens.MOTION_FADE_IN_MS,
            "shift_up_ms": design_tokens.MOTION_SHIFT_UP_MS,
            "resize_ms": design_tokens.MOTION_RESIZE_MS,
        },
        "residence": {
            "p50_sec": _r(_pct(values, 50)),
            "p10_sec": _r(_pct(values, 10)),
            "min_sec": _r(min(values)) if values else None,
            "max_sec": _r(max(values)) if values else None,
            "count": len(values),
            "still_visible_at_end": len(censored),
            "censored_ages_sec": censored_ages,
            "under_min_hold": under,
            "values_sec": values,
            "by_key_sec": {k: round(v, 3) for k, v in sorted(residence.items())},
        },
        "visible_pair_count": {
            "min": min(visible_counts) if visible_counts else 0,
            "p50": _r(_pct(visible_counts, 50), 1),
            "max": max(visible_counts) if visible_counts else 0,
            "histogram": {str(k): visible_hist[k] for k in sorted(visible_hist)},
        },
        "reflow": {
            "events": sum(s["events"] for s in by_kind.values()),
            "settext_changes": settext_changes,
            "visibility_changes": visibility_changes,
            "events_with_old_pair_y_jump": jump_events,
            "old_pair_y_jumps": jump_pairs,
            "max_old_pair_shift_px": _r(max_shift, 2),
            "reentries": reentries,
            "reentry_keys": sorted(reentry_keys),
            "by_kind": {k: {kk: (_r(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                        for k, v in by_kind.items()},
        },
        "ui_side_latency_ms": {
            "samples": len(latencies),
            "p50_ms": _r(_pct(latencies, 50)),
            "p90_ms": _r(_pct(latencies, 90)),
            "max_ms": _r(max(latencies)) if latencies else None,
            "by_kind": {k: {"n": len(v), "p50_ms": _r(_pct(v, 50)), "max_ms": _r(max(v))}
                        for k, v in latency_by_kind.items()},
            "note": "槽函数同步调用到 QLabel.setText 完成的墙钟耗时；离屏无 paint，作为 UI 侧对照",
        },
        "appear": {
            "fade_events": fade_events,
            "old_key_refades": old_key_refades,
        },
    }
    _suppress_construct(lambda: win.container.hide())
    return result


def _summary(result):
    c = result["config"]
    r = result["residence"]
    f = result["reflow"]
    v = result["visible_pair_count"]
    lat = result["ui_side_latency_ms"]
    lines = [
        f"配置：pairs-fit={c['pairs_fit']}  窗口={c['window_width']}x{c['window_height']}  "
        f"字号={c['font_size']}px  虚拟时长={c['virtual_duration_sec']}s",
        f"可见句对数：min={v['min']} p50={v['p50']} max={v['max']}  分布={v['histogram']}",
        f"每句驻留：p50={r['p50_sec']}s p10={r['p10_sec']}s min={r['min_sec']}s max={r['max_sec']}s  "
        f"n={r['count']}（期末仍在屏 {r['still_visible_at_end']}）",
        f"驻留 < minHold({c['min_hold_sec']}s) 的句数：{r['under_min_hold']}",
        f"重排：可见集合变化 {f['visibility_changes']}/{f['events']} 次事件；"
        f"QLabel 文本实际变化 {f['settext_changes']} 次",
        f"旧句 y 跳动：{f['events_with_old_pair_y_jump']} 个事件 / {f['old_pair_y_jumps']} 个旧句，"
        f"最大位移 {f['max_old_pair_shift_px']}px",
        f"旧句消失后重新出现：{f['reentries']} 次（{', '.join(f['reentry_keys']) or '无'}）",
        f"UI 侧首字延迟：p50={lat['p50_ms']}ms p90={lat['p90_ms']}ms max={lat['max_ms']}ms "
        f"(n={lat['samples']})",
        "按事件类型：" + "; ".join(
            f"{k}: events={s['events']} text={s['settext_changes']} "
            f"vis={s['visibility_changes']} old_y_jumps={s['old_pair_y_jumps']}"
            for k, s in sorted(f["by_kind"].items())
        ),
        f"淡入：次数={result['appear']['fade_events']}  "
        f"淡入了已上屏过的旧 key 的次数={result['appear']['old_key_refades']}",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="P4 出现体验 UI 侧基线探针")
    parser.add_argument("--json", metavar="PATH", help="把完整结果写到 JSON 文件")
    parser.add_argument("--pairs-fit", type=int, choices=(2, 6), default=2,
                        help="窗口高度大约只够显示几句（2 或 6）")
    args = parser.parse_args(argv)

    result = run(args.pairs_fit)
    print("=== P4 appear probe ===")
    print(_summary(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"JSON 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
