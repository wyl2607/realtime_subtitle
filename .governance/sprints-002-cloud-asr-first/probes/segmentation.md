# TK-001 分段探针

状态：**待协调者真机测量**。下面的表是空的。数字只从 jsonl 的 `summary` 行抄，不要凭这页的方法说明填。

判定（RFC「分段器两端不同」+ TK 卡片，门槛不改）：

- 采用词时间分段：`boundary_p90` ≤ 0.3s，并且 `end_latency_p50` ≤ 1.0s。
- 否则采用 A3'（能量 VAD + final 校正），阈值用 summary 里的 `a3_suggest_rms`。
- `verdict` 为 `incomplete`，或下面两份只跑完一份：结论写「不完整」。不要改判成词时间或 A3'。

两份都要跑，各看各的 summary。任一份 `boundary_p90` > 0.3 或 `end_latency_p50` > 1.0，总判定就是 A3'。

| 素材 | boundary_p90 | end_latency_p50 | pace_lag_p90 | clock_skew_s | verdict | a3_suggest_rms |
|---|---|---|---|---|---|---|
| concat5（n=5） | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 |
| FLEURS 60 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 | 待协调者真机测量 |

总判定：待协调者真机测量。

A3' 的阈值建议以 **FLEURS** 那份 `a3_suggest_rms` 为准（60 句）。concat5 只有 5 个句界，p90 几乎就是最差点，只作对照。两份建议不一致时两个都写上。

能量扫描档（写死，不是旋钮）：RMS 0.003 / 0.005 / 0.008 / 0.012 / 0.02，静音仍是 0.6s。建议取边界 p90 最小的一档，打平取更低的 RMS。能量事件在帧尾就「到达」，延迟大体就是 0.6s，**没有** final 校正。扫描回答的是哪档 RMS 更贴参照，不是词时间那条延迟。

## 参照是什么

拼接文件的参照 = 每一句原始 wav 在拼接轴上的起点（样本级精确匹配）+ 该 wav **内部**的人声起止。jsonl 里 `source` 为 `clip-align+acoustic-trim`。

声学修剪：20ms 帧 RMS，底噪取 p10，门限 `max(底噪×2.2, 底噪+0.0012)`，短于 0.08s 的起伏丢掉，一句取第一段人声的起点到最后一段人声的终点。句内停顿仍算同一句。这是参照，不是产品阈值。

FLEURS 一文件一句，各自新开一个 SpeechTranscriber。参照是该文件自己的声学修剪（`acoustic-trim`）。不要把 60 个文件接成一条时间轴。

`--bounds` 会盖掉上面的参照，`source` 变成 `bounds`。Silero 只走这条，见文末。正式结论用默认参照。

concat5 容器本身（量过文件，不是探针结论）：67.1s，16 kHz mono s16。前 5 条是 `wav/0000`–`0004`，句间和文件尾各有 1.0s 全零：

| id | 容器区间 (s) | audio_s |
|---|---|---|
| 0000 | 0–17.94 | 17.94 |
| 0001 | 18.94–30.10 | 11.16 |
| 0002 | 31.10–43.52 | 12.42 |
| 0003 | 44.52–55.20 | 10.68 |
| 0004 | 56.20–66.10 | 9.90 |

句界要用人声起止，容器边缘含句首句尾的静音。`0000` 很轻（20ms RMS：p50 ≈ 0.0018，p90 ≈ 0.012，max ≈ 0.034），固定 0.02 会漏掉这句的大部分帧，所以扫描里留了 0.003。concat5 整体 p10 ≈ 0.0004，p50 ≈ 0.0048，p90 ≈ 0.058。

## 词时间怎么收段

和 RFC 同一组数：末词之后静音 ≥ 0.6s 收段，单段 ≤ 15s 强切，短于 1.5s 且与下一段间隔在 (0.02s, 1.5s) 则合并。间隔为 0 的是强切接缝，不再粘回去。

决策时刻 = max(知道这个词尾的到达时刻, 词尾+0.6s)。迟到的 final 不能把已经到点的收段改早，否则延迟会被量短。文件放完后再喂 2s 静音，因为直播没有 EOF。

`boundary_p90` 是「参照→预测」和「预测→参照」两条误差放进**同一条**分布后的 p90（最近邻绝对误差）。分方向的 p50/p90 也在 summary 里。过切时 `boundary_pred_to_ref_p90` 会变大。判定用合并后的 `boundary_p90`。

分位和 rsbench 现有的一样：排序后 `index = Int((n-1)*q + 0.5)`。concat5 的 p90 样本很少，所以 FLEURS 那份才是分布。

延迟 = 预测段的 `decision_s` − 最近参照句的 `t1`（带符号）。中位数就是 `end_latency_p50`。

回放落后实时的 p90 > 0.2s，或识别时钟和文件时长差 > 0.05s，或结果里没有词级 `audioTimeRange`：程序把 verdict 收成 `incomplete`。

jsonl 只存时间和字符数 `n`，不存识别文本。

## 协调者命令

桌面会话里跑（Speech 要 GUI 会话，ssh 不行）。先停 Python 字幕和任何 rslite：两路一起开会抢 GPU，延迟无效；两个 rslite 还会互删采集设备。不要开 rslite 的 GUI / 采集模式。

```bash
bash "$HOME/projects/rs-mac-native/scripts/macos/stop.sh"
pgrep -lf rslite || echo "no rslite"

cd "$HOME/lanes/wt/rs-s2-tk001/macos-native"
export CLANG_MODULE_CACHE_PATH=/tmp/rs-s2-tk001-clang   # 仅 ~/.cache/clang 权限失败时需要
swift build -c release

# 约 70s 音频 + 2s 尾静音。第一次会在计时前下载语音资源。
caffeinate -i .build/release/rsbench probe-seg \
  --wav "$HOME/projects/rs-mac-native-data/concat5.wav" \
  --clips "$HOME/projects/rs-mac-native-data/wav" \
  --refs "$HOME/projects/rs-mac-native-data/refs.jsonl" \
  --n 5 --locale de-DE \
  --out "../.governance/sprints-002-cloud-asr-first/probes/seg-concat5.jsonl"

# 60 句，音频合计约 853s，再加每句 2s 尾静音，大约 16–20 分钟。
caffeinate -i .build/release/rsbench probe-seg \
  --wav-dir "$HOME/projects/rs-mac-native-data/wav" \
  --refs "$HOME/projects/rs-mac-native-data/refs.jsonl" \
  --locale de-DE \
  --out "../.governance/sprints-002-cloud-asr-first/probes/seg-fleurs.jsonl"
```

看门狗是「文件时长 + 2s + 60s」。第一句若卡在模型编译上会超时并标 `incomplete`。资源下过之后原命令重跑。那次超时不要填成 A3'。

自检（不碰麦克风、不加载模型）已经由执行者跑过：

```bash
cd "$HOME/lanes/wt/rs-s2-tk001/macos-native"
.build/release/rsbench probe-seg --selftest
```

### summary 里要抄的字段

`boundary_p90`，`end_latency_p50`，`end_latency_p90`，`pace_lag_p90`，`clock_skew_s`，`verdict`，`verdict_reason`，`a3_suggest_rms`，`a3_energy_sweep`，`volatile_run_width_p50` / `p90`，`final_run_width_p50` / `p90`，`n_results_without_word_time`，`ref_source`。

其它行：`result`（volatile/final 的到达时刻和每个 run 的 t0/t1/n）、`segment`（a0/a1/decision_s/reason）、`ref`。

## 可选：Silero 对照

不进上面那张判定表。模型第一次可能要下载。

```bash
PY="$HOME/projects/rs-mac-native/venv/bin/python"
SILERO="$HOME/lanes/wt/rs-s2-tk001/.governance/sprints-002-cloud-asr-first/probes/silero_bounds.py"
# 先确认脚本没把模型拉起来
"$PY" "$SILERO" --self-check

"$PY" "$SILERO" \
  --wav "$HOME/projects/rs-mac-native-data/concat5.wav" \
  --out /tmp/concat5-silero.jsonl
# concat5 不要加 --one-sentence：留 Silero 自己切的段。

"$PY" "$SILERO" \
  --wav-dir "$HOME/projects/rs-mac-native-data/wav" \
  --one-sentence \
  --out /tmp/fleurs-silero.jsonl
```

然后给 `probe-seg` 加上 `--bounds` 指向对应 jsonl，`--out` 换一个文件名。`ref_source` 应为 `bounds`。

## 实测结论（协调者，2026-10-11，MBA M2，SpeechAnalyzer de-DE 已安装）

| 素材 | 词时间分段 边界 p90 | 段尾延迟 p50 / p90 | 判定 |
|---|---|---|---|
| concat5（5 句，67s） | 4.90s | 2.18s / 3.02s | **A3'** |
| FLEURS 60 句 | 11.28s（pred→ref；ref→pred p90 1.28s） | 2.08s / 2.90s | **A3'** |

**根因**：volatile 结果没有词级时间——每条只有一个「段首→当前」的宽区间（volatile run 宽度 p50 6.8–8.4s）；精确的词时间只在 final 里出现，而 final 在段尾后约 2s 才到。用词时间判段尾，延迟就是 final 延迟，超过 1s 门槛。→ **TK-004 走 A3'：能量 VAD 收段 + final 词时间事后校正段边界**。

**A3' 能量扫描**（静音 ≥0.6s 收段）：

| rms | concat5 边界 p90 / 延迟 p50 / 段数(参照 5) | FLEURS 边界 p90 / 延迟 p50 / 段数(参照 60) |
|---|---|---|
| 0.003 | 2.34 / 0.60 / 7 | 10.22 / 0.60 / 99 |
| 0.005 | 1.96 / 0.58 / 6 | 10.68 / 0.58 / 100 |
| 0.008 | 4.98 / 0.56 / 7 | 11.26 / 0.58 / 104 |
| 0.012 | 6.40 / 0.54 / 8 | 11.40 / 0.58 / 119 |
| 0.020 | 5.16 / 0.38 / 8 | 11.58 / 0.52 / 136 |

- 判段延迟中位 ≈0.58s，满足「段尾→上屏 ≤2s」的预算（还剩 ~1.4s 给上传 + Groq p50 0.52s）。
- FLEURS 的大 p90 来自**句内停顿被切开**（60 句 → ~100 段），不是句尾漏切（ref→pred p90 1.28s）。对云端识别，句内切开不致命，但每段最低按 10s 计费：切得越碎越贵、上下文越短。
- **给 TK-004 的参数**：rms 取 **0.005**（concat5 最优、FLEURS 段数 1.67×）；保留 RFC 的 <1.5s 合并；段内仍按 ≤15s 强切。是否再把相邻短段合并到 ~8–10s 以压计费，留给 TK-004 用本表 + 67s 回放 WER 实测决定，不在探针里定。
