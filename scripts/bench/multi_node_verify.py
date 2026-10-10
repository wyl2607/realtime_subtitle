import json
import math
import re
import struct
import wave


ROUTE_SELECT_RE = re.compile(
    r"^rslite\.route select node=(?P<node>[^\s]+) score=(?P<score>-?\d+(?:\.\d+)?) "
    r"quality=(?P<quality>-?\d+(?:\.\d+)?) speed=(?P<speed>-?\d+(?:\.\d+)?) "
    r"penalties=(?P<penalties>-?\d+(?:\.\d+)?) bonus=(?P<bonus>-?\d+(?:\.\d+)?) "
    r"rtt_ms=(?P<rtt_ms>\d+(?:\.\d+)?)$"
)


def find_silence_windows(wav_path, threshold=0.003, min_duration=0.6):
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)
    samples = struct.unpack(f"<{len(frames)//2}h", frames)
    chunk_size = int(sr * 0.05)
    silences = []
    current = None
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i + chunk_size]
        if not chunk:
            break
        rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5 / 32768.0
        t = i / sr
        if rms < threshold:
            if current is None:
                current = [t, t + len(chunk) / sr]
            else:
                current[1] = t + len(chunk) / sr
        else:
            if current and current[1] - current[0] >= min_duration:
                silences.append((current[0], current[1]))
            current = None
    if current and current[1] - current[0] >= min_duration:
        silences.append((current[0], current[1]))
    return silences, nframes / sr


def silence_window_for(audio_t, silence_windows, epsilon=1e-6):
    if audio_t is None:
        return None
    for w0, w1 in silence_windows:
        if w0 - epsilon <= audio_t <= w1 + epsilon:
            return (w0, w1)
    return None


def _parse_events(stdout_text):
    events = []
    for line in stdout_text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def _node_src(event):
    text = event.get("text", "")
    if "A-seg" in text:
        return "A"
    if "B-seg" in text:
        return "B"
    return None


def _event_float(event, key):
    value = event.get(key) if event else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _wall_time_to_audio_t(events, wall_t):
    local_finals = [
        e for e in events
        if e.get("ev") == "final"
        and e.get("id") is not None
        and _event_float(e, "t") is not None
        and _event_float(e, "t1") is not None
    ]
    if not local_finals or wall_t is None:
        return None
    past = [e for e in local_finals if _event_float(e, "t") <= wall_t]
    ref = max(past, key=lambda e: _event_float(e, "t")) if past else min(
        local_finals, key=lambda e: abs(_event_float(e, "t") - wall_t)
    )
    return wall_t - _event_float(ref, "t") + _event_float(ref, "t1")


def _timed_overlap(left, right, tolerance=0.05):
    l0, l1 = left["t0"], left["t1"]
    r0, r1 = right["t0"], right["t1"]
    if None in (l0, l1, r0, r1):
        return False
    return min(l1, r1) - max(l0, r0) > tolerance


def verify_probe(stdout_text, stderr_text, gateway_log, drained_events, wav_file, rc, token_a, token_b):
    results = {"pass": 0, "fail": 0, "details": []}

    def check(ok, label):
        if ok:
            results["pass"] += 1
            results["details"].append({"check": label, "result": "PASS"})
            print(f"  ✓ {label}")
        else:
            results["fail"] += 1
            results["details"].append({"check": label, "result": "FAIL"})
            print(f"  ✗ {label}")

    try:
        silence_windows, wav_duration_sec = find_silence_windows(wav_file)
        wav_read_error = None
    except Exception as exc:  # noqa: BLE001 - verification reports the failure below.
        silence_windows, wav_duration_sec = [], None
        wav_read_error = type(exc).__name__

    tokens = [t for t in (token_a, token_b) if t]
    check(not any(t in stdout_text for t in tokens), "Tokens are absent from stdout")
    check(not any(t in stderr_text for t in tokens), "Tokens are absent from stderr")
    check(not any(t in gateway_log for t in tokens), "Tokens are absent from gateway log")

    route_logs = [line for line in stderr_text.splitlines() if line.startswith("rslite.route")]
    score_logs = [line for line in route_logs if ROUTE_SELECT_RE.match(line)]
    print(f"\nRouting score logs ({len(score_logs)} lines):")
    for line in score_logs:
        print(f"  {line}")

    events = _parse_events(stdout_text)
    finals = [e for e in events if e.get("ev") == "final"]
    node_finals = [e for e in finals if e.get("id") is None]
    a_finals = [e for e in node_finals if _node_src(e) == "A"]
    b_finals = [e for e in node_finals if _node_src(e) == "B"]

    check(rc == 0, f"rslite exit code 0 (actual: {rc})")

    select_node_lines = [line for line in route_logs if line.startswith("rslite.route select node=")]
    all_select_matched = len(select_node_lines) > 0 and all(ROUTE_SELECT_RE.match(line) for line in select_node_lines)
    check(all_select_matched, "Routing select logs fully match regex with all fields")

    select_nodes = [ROUTE_SELECT_RE.match(line).group("node") for line in score_logs]
    idx_2nd_b = -1
    for i in range(len(select_nodes) - 1):
        if select_nodes[i] == "node-b" and select_nodes[i + 1] == "node-b":
            idx_2nd_b = i + 1
            break
    check(idx_2nd_b != -1, "Migration preceded by at least two consecutive select node=node-b")

    status_b_events = [e for e in events if e.get("ev") == "status" and "混合·node-b" in e.get("text", "")]
    check(len(status_b_events) > 0, "stdout contains status '混合·node-b'")

    first_b = min(
        (e for e in b_finals if _event_float(e, "t0") is not None),
        key=lambda e: _event_float(e, "t0"),
        default=None,
    )
    first_b_t0 = _event_float(first_b, "t0")
    prev_a = max(
        (
            e for e in a_finals
            if _event_float(e, "t1") is not None
            and first_b_t0 is not None
            and _event_float(e, "t1") <= first_b_t0 + 0.25
        ),
        key=lambda e: _event_float(e, "t1"),
        default=None,
    )
    prev_a_t1 = _event_float(prev_a, "t1")
    a_window = silence_window_for(prev_a_t1, silence_windows)
    b_window = silence_window_for(first_b_t0, silence_windows)
    matching_silence = a_window if a_window is not None and a_window == b_window else None
    switch_points_agree = (
        prev_a_t1 is not None and first_b_t0 is not None
        and abs(prev_a_t1 - first_b_t0) <= 0.25
    )
    check(
        matching_silence is not None and switch_points_agree,
        f"Migration audio switch A.t1={prev_a_t1} and B.t0={first_b_t0} fall in the same WAV silence window >= 0.6s ({matching_silence})",
    )

    check(len(drained_events) > 0, f"Old session received drain and emitted drained ({len(drained_events)} times)")
    drained_audio_s = [
        e.get("audio_s") for e in drained_events
        if isinstance(e, dict) and isinstance(e.get("audio_s"), (int, float)) and not isinstance(e.get("audio_s"), bool)
    ]
    drained_ok = (
        matching_silence is not None
        and any(matching_silence[0] <= audio_s <= matching_silence[1] for audio_s in drained_audio_s)
    )
    check(drained_ok, f"Drained audio_s falls in migration window ({drained_audio_s})")

    check(
        matching_silence is not None and first_b_t0 is not None
        and matching_silence[0] <= first_b_t0 <= matching_silence[1],
        f"Node B first final t0 ({first_b_t0}) falls in the same silence window >= window_start",
    )

    fallback_idx = next((idx for idx, line in enumerate(route_logs) if "fallback failed_node=node-b" in line), None)
    select_a_after = False
    if fallback_idx is not None:
        select_a_after = any("select node=node-a" in line for line in route_logs[fallback_idx + 1:])
    check(fallback_idx is not None and select_a_after, "Fallback failed_node=node-b is followed by select node=node-a")

    last_b_t1 = max((_event_float(f, "t1") for f in b_finals if _event_float(f, "t1") is not None), default=0.0)
    a_finals_after_b = [f for f in a_finals if _event_float(f, "t0") is not None and _event_float(f, "t0") >= last_b_t1]
    check(len(a_finals_after_b) > 0, f"Node A final after fallback has t0 >= last Node B t1 (last B t1={last_b_t1:.2f})")

    last_b_wall_t = max((_event_float(e, "t") for e in b_finals if _event_float(e, "t") is not None), default=None)
    fallback_status = next(
        (
            e for e in events
            if e.get("ev") == "status"
            and _event_float(e, "t") is not None
            and (last_b_wall_t is None or _event_float(e, "t") > last_b_wall_t)
            and ("模式：本机" in e.get("text", "") or "混合·node-a" in e.get("text", ""))
        ),
        None,
    )
    fallback_audio_t = _wall_time_to_audio_t(events, _event_float(fallback_status, "t")) if fallback_status else None
    if matching_silence and fallback_audio_t is not None:
        b_active_duration = max(0.0, fallback_audio_t - matching_silence[0])
        expected_b_count = math.floor(b_active_duration / 5.0)
        check(
            abs(len(b_finals) - expected_b_count) <= 2,
            f"Node B count ({len(b_finals)}) matches expected (~{expected_b_count}) using fallback audio_t={fallback_audio_t:.2f}",
        )
    else:
        check(False, "Could not verify Node B count due to missing migration window or fallback audio position")

    node_finals.sort(key=lambda x: (_event_float(x, "t0") is None, _event_float(x, "t0") or 0.0))
    phases = []
    for nf in node_finals:
        src = _node_src(nf)
        if src is None:
            continue
        if not phases or phases[-1]["src"] != src:
            phases.append({"src": src, "count": 1, "first": nf})
        else:
            phases[-1]["count"] += 1
    phase_pattern = [p["src"] for p in phases]
    check(phase_pattern == ["A", "B", "A"], f"Node sentences follow time order Phase A -> Phase B -> Phase A")
    b_phase = next((p for p in phases if p["src"] == "B"), None)
    b_phase_t0 = _event_float(b_phase["first"], "t0") if b_phase else None
    check(
        matching_silence is not None and b_phase_t0 is not None
        and matching_silence[0] <= b_phase_t0 <= matching_silence[1],
        f"Phase B starts in the migration silence window (t0={b_phase_t0}, window={matching_silence})",
    )

    class LineStore:
        def __init__(self):
            self.lines = []
            self.next_node_key = -1

        def should_replace(self, line, n0, n1):
            if line["source"] != "local":
                return False
            if n0 is None or n1 is None:
                return False
            l0, l1 = line["t0"], line["t1"]
            if l0 is None or l1 is None:
                return False
            duration = max(l1 - l0, 0)
            if duration <= 1e-9:
                return l0 >= n0 - 1e-9 and l0 <= n1 + 1e-9
            overlap = min(l1, n1) - max(l0, n0)
            return overlap >= duration * 0.5 - 1e-9

        def add_local(self, key, t0, t1, text):
            line = {"key": key, "t0": t0, "t1": t1, "source": "local", "text": text}
            for ext in self.lines:
                if ext["source"] == "node" and self.should_replace(line, ext["t0"], ext["t1"]):
                    return
            self.lines.append(line)

        def apply_node(self, t0, t1, text):
            replaced = [line["key"] for line in self.lines if self.should_replace(line, t0, t1)]
            self.lines = [line for line in self.lines if line["key"] not in replaced]
            key = self.next_node_key
            self.next_node_key -= 1
            line = {"key": key, "t0": t0, "t1": t1, "source": "node", "text": text}
            if t0 is not None:
                inserted = False
                for i in range(len(self.lines)):
                    ext_t0 = self.lines[i]["t0"]
                    if ext_t0 is not None and ext_t0 > t0:
                        self.lines.insert(i, line)
                        inserted = True
                        break
                if not inserted:
                    last_timed = -1
                    for i in range(len(self.lines) - 1, -1, -1):
                        if self.lines[i]["t0"] is not None:
                            last_timed = i
                            break
                    if last_timed != -1:
                        self.lines.insert(last_timed + 1, line)
                    else:
                        self.lines.append(line)
            else:
                self.lines.append(line)

    store = LineStore()
    for ev in events:
        if ev.get("ev") == "final":
            fid = ev.get("id")
            if fid is not None:
                store.add_local(fid, ev.get("t0"), ev.get("t1"), ev.get("text"))
            else:
                store.apply_node(ev.get("t0"), ev.get("t1"), ev.get("text"))

    visible_lines = store.lines
    check(len(visible_lines) > 0, "Visible track is not empty")

    local_keys = [line["key"] for line in visible_lines if line["source"] == "local"]
    check(len(local_keys) == len(set(local_keys)) and all(local_keys[i] < local_keys[i + 1] for i in range(len(local_keys) - 1)), "Local IDs strictly increasing and not empty")
    check(len(local_keys) > 0, "Local ID list is not empty")

    node_lines = [line for line in visible_lines if line["source"] == "node"]
    node_texts = [line["text"] for line in node_lines]
    check(len(node_texts) == len(set(node_texts)), "Node sentences have strictly unique text")

    node_overlap = any(_timed_overlap(node_lines[i], node_lines[j]) for i in range(len(node_lines)) for j in range(i + 1, len(node_lines)))
    check(not node_overlap, "Node sentences do not overlap")

    local_lines = [line for line in visible_lines if line["source"] == "local"]
    local_overlap = any(_timed_overlap(local_lines[i], local_lines[j]) for i in range(len(local_lines)) for j in range(i + 1, len(local_lines)))
    check(not local_overlap, "Local sentences do not overlap")

    max_gap = 4.0
    excess_gaps = []
    if wav_duration_sec is None:
        check(False, f"Timeline coverage could not read WAV duration ({wav_read_error})")
    else:
        timed_lines = []
        for line in visible_lines:
            if line["t0"] is None or line["t1"] is None:
                continue
            t0 = max(0.0, line["t0"])
            t1 = min(wav_duration_sec, line["t1"])
            if t1 < 0.0 or t0 > wav_duration_sec or t1 < t0:
                continue
            timed_lines.append({**line, "t0": t0, "t1": t1})
        timed_lines.sort(key=lambda x: x["t0"])
        if timed_lines:
            if timed_lines[0]["t0"] > max_gap:
                excess_gaps.append((0.0, timed_lines[0]["t0"]))
            for i in range(len(timed_lines) - 1):
                if timed_lines[i + 1]["t0"] - timed_lines[i]["t1"] > max_gap:
                    excess_gaps.append((timed_lines[i]["t1"], timed_lines[i + 1]["t0"]))
            if wav_duration_sec - timed_lines[-1]["t1"] > max_gap:
                excess_gaps.append((timed_lines[-1]["t1"], wav_duration_sec))
        check(len(timed_lines) > 0 and len(excess_gaps) == 0, f"Timeline coverage covers 0 to {wav_duration_sec:.1f}s without gaps > 4.0s")

    check("switch_failed" not in stderr_text, "No switch_failed in stderr")

    total = results["pass"] + results["fail"]
    print("\n=== SUMMARY ===")
    print(f"Passed: {results['pass']}/{total}")
    if results["fail"] == 0:
        return 0
    return 1
