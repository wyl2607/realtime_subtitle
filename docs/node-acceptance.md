# 节点化（remote-offload）端到端验收记录

> 状态：骨架（TK-007 准备阶段）。所有 `TODO` 待 TK-005 合并、mini2 节点就绪后由 Coordinator 在真机补证据。
> 契约来源：`.governance/sprints-001-remote-offload/RFC.md` 的 Done criteria 1–8。
> 每条证据都写：命令、输出摘要、数字、日期/commit。没跑过的不要填。

## 已知基线（HANDOFF §2，同一段 67s concat5 回放）

| 档位 | WER | 译文延迟 | 整机功耗（MacBook Air，CPU+GPU+ANE） |
|---|---|---|---|
| 空闲 | — | — | 1004 mW |
| B（本机系统识别 + 系统翻译） | 7.9% | 2.2–4.5 s | 1124 mW |
| C（v1 远程 Whisper，客户端侧） | 2.6% | 5.6–12.3 s（系统翻译后 7–11 s） | 822 mW |

三者功耗差异都在噪声以内。concat5 只对应 `refs.jsonl` 前 5 句（共 60 句），评分必须按前 5 句。

## 1. 一键安装

- 命令：`scripts/node/install_node.sh mini2`（第二次执行验证幂等；`--local`；人为制造自检失败验证回滚）
- 检查项：mini2 开机自启；两端 token 权限 0600；客户端 `~/.config/rslite/nodes.json` 已写入
- 证据：TODO

## 2. 按需加载

- 检查项：无会话时 mini2 上无 worker 进程、gateway 常驻内存 < 50MB；会话结束 120s 后 worker 退出；客户端被 kill/断网后 30s 内判定断开并回收 worker；冷启动后首条精修 ≤ 15s
- 命令（控制端 MacBook 执行；远端只读 `ps` 与 gateway 日志，不停止/杀远端进程）：
  ```bash
  bash scripts/bench/node_lifecycle_probe.sh mini2 ~/projects/rs-mac-native-data/concat5.wav
  ```
- 判定：
  - `idle_no_worker`：`ssh mini2 ps -axo pid,rss,command` 中没有 `realtime_subtitle.node.worker`。
  - `idle_gateway_rss`：`realtime_subtitle.node.gateway` 的最大 RSS < 50 MB。
  - `cold_first_refine`：本机 `rslite --headless --source file:<wav> --mode hybrid` 启动后，stdout JSONL 中首条节点精修结果 ≤ 15s；脚本只保留事件类型和时间戳，不记录字幕正文。
  - `idle_reap_after_playback`：回放结束后 worker 在约 120s 保温窗口后退出，脚本判定窗口为 100–150s。
  - `disconnect_detected`：第二次启动 rslite，观察到 worker 出现后 kill 本机 rslite；用 gateway `session_end dur_s` 与本机 kill 时刻相减，要求 ≤ 30s。
  - `disconnect_worker_reap`：kill 后 worker 最迟 150s 内消失（30s 断线判定 + 120s 保温回收上限）。
  - stdout 每项输出 PASS/FAIL 与数字，最后一行是汇总 JSON；实测数字填入下方证据。
- 证据：TODO（本探针不需要远端 kill/停服务；若另做远端故障注入须先获用户批准）

## 3. 自动选档

- 检查项：MacBook Air（不满足 P8 门槛）连得上 mini2 时进混合档；停掉 mini2 gateway 后自动退回本机 B、字幕不断；mini2 本机运行 rslite 时走 UDS、无 token、不经网络
- 命令：TODO（`rslite --mode auto`，观察「本机 / 混合·节点 / 精修中」显示与日志中的选择理由）
- 证据：TODO

## 4. 混合档（concat5，67s 回放）

- 命令（待 TK-005 合并后实跑）：
  ```bash
  rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > /tmp/tk007/hybrid.events
  rslite --mode local  --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > /tmp/tk007/b.events
  venv/bin/python scripts/bench/hybrid_score.py /tmp/tk007/hybrid.events \
      --refs $HOME/projects/rs-mac-native-data/refs.jsonl --b-events /tmp/tk007/b.events --c-wer 0.026
  ```
  `power_compare.sh` 跑完也会留下 `hybrid.events`，可直接评分。
- 注意：`hybrid_score.py` 假设节点精修行的事件带 `source` 以 `node` 开头，并有 `t0/t1`（P4 客户端时钟）；TK-005 的 headless 输出字段若不同，先对齐脚本再取数。

| 指标 | RFC 要求 | 实测 | 结论 |
|---|---|---|---|
| 首字时间 | 不慢于 B | TODO | TODO |
| 最终文本 WER | ≤ C + 1pp（C 基线 2.6% → ≤ 3.6%） | TODO | TODO |
| 精修延迟中位数 | ≤ 4 s | TODO | TODO |
| 不重复、不丢句 | 无 | TODO | TODO |

- 评分脚本已用 B 档历史数据（`rslite_fast2.jsonl`，5 句、无节点行）验证能跑通：最终 WER 0.0789（与基线 7.9% 一致）；该数据没有节点行，所以精修延迟为 n/a。

## 5. 多节点路由

- 检查项：mini2 + 本机模拟的第二节点；更优节点上线后在静音点迁移、下线后退回；字幕不断不重复；选择理由在日志可见
- 命令：
  ```bash
  # 生成带静音点的德语测试音频（约 105s）
  say -v Anna "Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das Echtzeit-Untertitel-System. [[slnc 1000]] Wir prüfen die Mehrknoten-Routing-Funktionalität. [[slnc 1000]] Der erste Knoten hat eine langsamere Erkennung. [[slnc 1000]] Der zweite Knoten ist schneller und kommt später online. [[slnc 1000]] Danach geht der zweite Knoten wieder offline. [[slnc 1000]] Und wir fallen zurück auf den ersten Knoten. [[slnc 1000]] Das System soll nahtlos im Stille-Punkt migrieren. [[slnc 1000]] Ohne Text-Wiederholungen oder Verluste. [[slnc 1000]] Wir wiederholen den Text mehrmals. [[slnc 1000]] Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das Echtzeit-Untertitel-System. [[slnc 1000]] Wir prüfen die Mehrknoten-Routing-Funktionalität. [[slnc 1000]] Der erste Knoten hat eine langsamere Erkennung. [[slnc 1000]] Der zweite Knoten ist schneller. [[slnc 1000]] Und kommt später online. [[slnc 1000]] Danach geht der zweite Knoten wieder offline. [[slnc 1000]] Und wir fallen zurück. [[slnc 1000]] Auf den ersten Knoten. [[slnc 1000]] Das System soll nahtlos migrieren. [[slnc 1000]] Ohne Wiederholungen. [[slnc 1000]] Oder Verluste. [[slnc 1000]] Noch eine Runde. [[slnc 1000]] Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das System. [[slnc 1000]] Wir prüfen das Routing. [[slnc 1000]] Erster Knoten langsam. [[slnc 1000]] Zweiter Knoten schnell. [[slnc 1000]] Zweiter geht offline. [[slnc 1000]] Zurück zum ersten. [[slnc 1000]] Migriert im Stille-Punkt. [[slnc 1000]] Keine Wiederholungen. [[slnc 1000]] Keine Verluste." -o /tmp/german_test.aiff
  afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/german_test.aiff /tmp/german_test.wav
  
  # 运行验收探针（自动起两个假网关、模拟节点上下线、跑 rslite --mode auto）
  uv run python scripts/bench/multi_node_probe.py
  ```
- 判定（脚本自动输出 PASS/FAIL 与汇总 JSON）：
  - `rslite exit code 0`：rslite 正常跑完整个回放
  - `Token A not in output` / `Token B not in output`：token 未泄露到 stdout/stderr/gateway 日志
  - `Routing logs contain selection reasons`：stderr 出现 `rslite.route select node=... quality=... speed=... penalties=... bonus=... rtt_ms=...` 形式的打分日志
  - `Routing logs show migration to node-b`：出现 `select node=node-b` 迁移到更优节点
  - `Fallback to node-a after node-b offline logged`：节点下线后出现 `fallback failed_node=node-b` 或重新 `select node=node-a`
  - `Final event IDs non-decreasing`：final 事件 id 单调不减
  - `Received finals from node A/B`：两个节点都有产出 final
  - `Sufficient total finals`：总 final 数量与音频时长相符
  - `No switch_failed in stderr`：无切换失败
  - 末尾汇总 JSON 含 `checks_passed`/`checks_failed`/`final_ids`/`node_a_finals`/`node_b_finals` 等字段
- 证据：TODO（实测时填入）

## 6. 功耗（五组）

- 命令（用户 sudo 执行；测前停掉 A 版和浏览器视频，脚本会检查并拒绝在 A 版/rslite 已运行时开始）：
  ```bash
  sudo bash scripts/bench/power_compare.sh ~/projects/rs-mac-native-data/concat5.wav
  ```
- 每组约 76 秒（含 8s 收尾），组间冷却 30s；A 版另加 30s 模型预热。建议整套至少重复 2 轮估计噪声。

| 组 | 平均功耗 (mW) 第 1 轮 | 第 2 轮 | 备注 |
|---|---|---|---|
| 空闲 | TODO | TODO | 基线 1004 |
| B（rslite 本机） | TODO | TODO | 基线 1124 |
| C（远程精修，v2 节点） | TODO | TODO | 基线 822（v1）；脚本里用 `--mode auto`，若客户端上与混合无法区分则改为节点侧采样 |
| A（Python 桌面字幕） | TODO | TODO | 用 afplay 放音频，A 版抓系统声音；afplay 自身有少量播放功耗，其它组不出声 |
| 混合（rslite --mode hybrid） | TODO | TODO | |

- 噪声说明：TODO（两轮之差；已知空闲与 B 仅差 120mW，在噪声以内——差距小于重复测量波动时不下结论）

## 7. 质量门

- 命令：`QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest -q -p no:cacheprovider`；`venv/bin/ruff check .`；`cd macos-native && swift build -c release --product rslite`
- 检查项：P2 各项上限、S3、S7 有测试覆盖；每个 TK 至少 2 轮评审（鉴权/生命周期/状态机类 3 轮）
- 证据：TODO（云端基线参考：916 passed / 46 skipped / 8 个 Windows 专用收集错误）

## 8. 文档

- 检查项：`CLAUDE.md` 第 7 节新增避坑条目；README 说明用法；`docs/protocol-v2.md` 写清与 v1 的差异（TK-006）
- 证据：TODO

## 不达标项与后续

TODO（不达标的要么回对应 TK 修，要么在此写明原因与后续计划）
