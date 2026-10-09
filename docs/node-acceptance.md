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
- 命令：TODO（`ps`/`vmmap` 取内存；kill 客户端；计时）
- 证据：TODO（kill worker / 停 gateway 等远程操作须先获用户批准）

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

- 检查项：mini2 + 本机模拟的第二节点（`--local` 安装或另一端口 gateway）；更优节点上线后在静音点迁移、下线后退回；字幕不断不重复；选择理由在日志可见
- 命令：TODO
- 证据：TODO

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
