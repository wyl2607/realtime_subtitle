"""Pure-Python contract tests for the shared subtitle history."""
import time
from realtime_subtitle.ui import design_tokens
from realtime_subtitle.ui.history_store import SubtitleHistoryStore, rows_to_srt


def test_add_same_key_updates_and_generated_keys_are_private():
    store = SubtitleHistoryStore()
    first = store.add("Guten Tag", "你好", key="line-1", wall=10)
    same = store.add("Guten Tag!", "您好", key="line-1", wall=99)
    generated = store.add("Ohne", "无", wall=11)
    assert first is same
    assert [(row.key, row.src, row.dst, row.wall) for row in store.rows()] == [
        ("line-1", "Guten Tag!", "您好", 10.0),
        (generated.key, "Ohne", "无", 11.0),
    ]
    assert generated.key.startswith("@n:")


def test_capacity_trims_oldest_and_emits_delete_reason():
    store = SubtitleHistoryStore(capacity=2)
    events = []
    store.subscribe(events.append)
    store.add("一", "1", key="a")
    store.add("二", "2", key="b")
    store.add("三", "3", key="c")
    assert [row.key for row in store.rows()] == ["b", "c"]
    assert events[-1].kind == "delete"
    assert events[-1].keys == ("a",)
    assert events[-1].reason == "trim"


def test_update_drop_clear_and_language_events_are_exact():
    store = SubtitleHistoryStore()
    events = []
    store.subscribe(events.append)
    store.add("Quelle", "目标", key="x")
    assert not store.update("x", dst="目标")
    assert store.update("x", src="Quelle 2", dst="目标 2")
    assert events[-1].fields == frozenset({"src", "dst"})
    assert store.drop(["missing", "x"]) == ["x"]
    assert events[-1].reason == "drop"
    store.mark_language("de", "zh", 3)
    assert store.language_label(3) == "de→zh"
    assert events[-1].kind == "language"
    store.clear()
    assert events[-1].kind == "reset"


def test_move_uses_t0_order_and_reports_source_change_separately():
    store = SubtitleHistoryStore()
    events = []
    store.subscribe(events.append)
    store.add("none", "无", key="none", t0=None)
    store.add("late", "晚", key="late", t0=5)
    store.add("early", "早", key="early", t0=1)
    assert store.move_by_t0("early", source="remote")
    assert [row.key for row in store.rows()] == ["none", "early", "late"]
    assert events[-2].kind == "move" and events[-2].index == 1
    assert events[-1].fields == frozenset({"source"})
    assert store.get("early").source == "remote"
    assert not store.move_by_t0("missing")


def test_search_text_and_statuses():
    store = SubtitleHistoryStore()
    ok = store.add("Haus", "房子", key="ok", wall=0)
    failed = store.add("Baum", design_tokens.TEXT_UNTRANSLATED, key="failed", wall=1)
    retrying = store.add("Weg", design_tokens.TEXT_TRANSLATING, key="retry", wall=2)
    assert [row.key for row in store.search("房")] == ["ok"]
    assert store.search("  ") == store.rows()
    assert [ok.status, failed.status, retrying.status] == ["ok", "failed", "retrying"]
    assert store.to_text([ok]) == f"[{time.strftime('%H:%M:%S', time.localtime(0))}] Haus\n房子\n"


def test_srt_modes_hide_non_final_translations_and_make_time_monotonic():
    store = SubtitleHistoryStore()
    first = store.add("Erste", "第一", key="a", t0=2, t1=4, wall=100)
    failed = store.add("Zweite", design_tokens.TEXT_UNTRANSLATED, key="b", t0=3, t1=3, wall=101)
    retrying = store.add("Dritte", design_tokens.TEXT_TRANSLATING, key="c", wall=102)
    bilingual = rows_to_srt([first, failed, retrying])
    assert "Erste\n第一" in bilingual
    assert design_tokens.TEXT_UNTRANSLATED not in bilingual
    assert design_tokens.TEXT_TRANSLATING not in bilingual
    target = store.to_srt(mode="target")
    source = store.to_srt(mode="source")
    assert "第一" in target and "Zweite" in target
    assert "第一" not in source and "Erste" in source
    assert bilingual.index("00:00:02,000") < bilingual.index("00:00:04,000")
    assert rows_to_srt([]) == ""


def test_listener_failure_does_not_block_later_listener(capsys):
    store = SubtitleHistoryStore()
    observed = []

    def broken(_event):
        raise RuntimeError("boom")

    store.subscribe(broken)
    store.subscribe(observed.append)
    store.add("Hallo", "你好")
    assert observed[0].kind == "insert"
    assert "⚠️" in capsys.readouterr().out


def test_zero_is_a_real_key_not_missing():
    store = SubtitleHistoryStore()
    store.add("Null", "零", key=0)
    assert store.get(0) is not None and store.get("0").dst == "零"
    store.add("Null", "零号", key=0)
    assert len(store) == 1 and store.get(0).dst == "零号"
