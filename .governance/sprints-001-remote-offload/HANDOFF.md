# 交接包 — realtime_subtitle 外包算力版（sprints-001-remote-offload）

> 写于 2026-10-08 22:15 CEST，由 Claude（Coordinator）在额度可能耗尽前写成。接手的 AI 读完本文件，不需要再找用户补充背景。
> 本文件有三份副本：本机仓库 `~/projects/rs-mac-native/.governance/sprints-001-remote-offload/HANDOFF.md`、mini2 上的 `~/rs-HANDOFF.md`、本机记忆 `~/.claude/projects/-/memory/handoff-2026-10-08-rs-remote-offload.md`。

## 1. Goal

推进 SDD Sprint `sprints-001-remote-offload`，按队列完成 TK-001 到 TK-007，产品目标如下：
- rslite（macOS 原生字幕）在本机用省电小模型立即出字，也就是系统识别加系统翻译。
- 同时让最合适的节点（目前是 mini2，下一期加 Windows 主机）用大模型（Whisper turbo）对同一段音频做「整段精修」，按音频时间把本机那几句替换掉。
- 节点要能一键安装，平时不占资源，有会话时才加载模型，用完自动释放。

## 2. Context

| 项 | 值 |
|---|---|
| 主仓库 / 分支 | `~/projects/rs-mac-native`（git worktree），分支 `feat/macos-native`，HEAD `1ca42d0`，**没有 push 过** |
| 原始仓库 | `~/projects/realtime_subtitle`（master，GitHub wyl2607/realtime_subtitle） |
| 需求 / 设计 / 评审 | `.governance/specs/001-remote-offload-productization/{requirement,draft,draft.review}.md` |
| Sprint 五件套 | `.governance/sprints-001-remote-offload/{RFC,PLAN,TASKS,CURSOR,REVIEWS}.md`，`tasks/TK-00*.md`，`metrics/phase-trace.jsonl` |
| **契约** | RFC 的 P1–P8 已冻结，**不许改**（P1 info、P2 会话 v2、P3 gateway↔worker 管道、P4 带时间句子、P5 替换规则、P6 节点清单、P7 路由、P8 本机档门槛） |
| Python venv | `~/projects/rs-mac-venv`（rs-mac-native 里的 `venv` 是指向它的符号链接），已装 websockets 17.2 |
| 基准数据 | `~/projects/rs-mac-native-data/`：`concat5.wav` 是 67s 的 5 段德语，`refs.jsonl` 是参考文本，`wav/`、`de_zh_pairs.json` 是旧基线；含 jiwer 的 python 在 `/private/tmp/claude-501/-Users-yumei-projects-realtime-subtitle/efb5c207-…/scratchpad/bench-venv/bin/python`（在 /tmp 下，随时可能被清掉，没了就用 uv 重建并装 jiwer） |

### 已完成并验证（都在 feat/macos-native 上）

- **A 版（Python 桌面字幕）**：Mac 上的翻译改走系统 Translation（`rstranslate` helper，`TRANSLATE_BACKEND=auto`）；修过 MLX 跨线程 bug（d9d6f5d：所有 MLX 调用都在单线程执行器里）。
- **B 版 rslite**：系统识别加 `.fastResults`，Process Tap 抓系统声音；`main` 必须是同步函数（5b1a3c5）；`SentenceCommitter` 逐句提前定稿（ea5d4af）。
- **C 版 v1**：远程服务端 `realtime_subtitle/remote/server.py`，rslite 用 `--remote` 连接，连不上时回退本机。v1 会在 TK-006 删除。
- **实测数据**（同一段 67s 音频）：
  - B：WER 7.9%，译文延迟 2.2–4.5s
  - C：WER 2.6%，延迟 5.6–12.3s；改用系统翻译后 7–11s
  - 功耗（MacBook Air 整机，CPU+GPU+ANE）：空闲 1004mW、B 1124mW、C 822mW，三者差异都在噪声以内

### Sprint 当前状态（以 CURSOR.md 为准）

| TK | 状态 | 执行者 / worktree | 说明 |
|---|---|---|---|
| TK-001 节点 worker | **executing** | Claude sonnet 子代理，`~/projects/rs-tk-001`（feat/tk-001） | codex 额度耗尽、零产出后改派。写本文件时子代理刚开工，worktree 里还没有改动 |
| TK-004 rslite 时间+混合+单实例 | **reviewing（CR-001 第 1 轮，Opus 评审中）** | `~/projects/rs-tk-004`，提交 ecb7a20 + 0cdd5c7 | 实现在 write_scope 之内；main/RemotePipeline 只由 coordinator-direct 改了 onFinal 签名（0cdd5c7），单实例接线和模式文案留给 TK-005。看 `reviews/CR-001.md`，评审员结论出来后由 Coordinator 分诊 |
| TK-002 gateway | planned | — | 安全类，单独一批 |
| TK-003 install_node.sh | planned | — | 涉及 token，单独一批 |
| TK-005 NodeClient+Router | planned | 等 TK-004 | |
| TK-006 清理 v1 + 文档 | planned | 等 001/002/005 | |
| TK-007 验收 + 五组功耗 | planned | 最后，单独一批 | 远程操作，**用户已批准** |

> 如果 Claude 的额度在子代理运行途中耗尽，子代理也会一起停。接手时先看两个 worktree 的 `git status` 和 `git log`：有 commit 就进入评审；只有未提交的改动就判断是继续写还是重做。

### 节点 / 机器状态

- **mini2**（ssh 别名 `mini2`，用户 yilinwang，M4 16GB，macOS 27，Tailscale IP 100.105.163.59）：
  - v1 服务正在运行：PID 34924，`~/rs-remote`，监听 `100.105.163.59:8791`，使用系统翻译。是用 nohup 起的，**不是 launchd**，重启机器就没了。
  - token 在 `~/.config/rs-remote/token`（0600）。
  - 德→中翻译语言包已经 installed（用户通过屏幕共享下载）。
  - ⚠️ `~/projects` 是指向未挂载外置盘「Mac扩容」的**悬空软链接**，不要往里放东西；一律用内置盘的 `~/rs-*`。
  - ⚠️ 8790 端口被 iphone 采集服务占用（PID 1712），不要碰。
  - mini2 **没有开自动登录**，要不要开由用户决定。屏幕共享已开（5900 端口）。
- **本机 MacBook Air M2 16GB**：
  - 客户端 token 在 `~/.config/rslite/remote-token`（0600）。
  - 本机跑 Whisper 会过热。测试 B 时不要同时开着 A。
  - 本机没有 GNU `timeout`；agy 在 headless 模式下会拒绝所有 shell 命令，**不要派 agy**。

## 3. Relevant files（按 TK 卡片的 write_scope，越窄越好）

- TK-001：`realtime_subtitle/node/{worker,segmenter,engines}.py`，`tests/test_node_{worker,segmenter}.py`
- TK-002：`realtime_subtitle/node/{gateway,info,__init__}.py`，`realtime_subtitle/node/requirements.txt`，`tests/test_node_gateway.py`
- TK-003：`scripts/node/**`
- TK-004：`macos-native/Sources/rslite/{Pipeline,Overlay,AudioFanout,SingleInstance,SentenceCommitter}.swift`
- TK-005：`macos-native/Sources/rslite/{NodeClient,NodeRouter,HybridEngine,Capability,main}.swift`；删除 `RemotePipeline.swift`；`macos-native/README.md`
- TK-006：删除 `realtime_subtitle/remote/**` 和 `tests/test_remote_server.py`；修改 `CLAUDE.md`；新增 `docs/protocol-v2.md`
- TK-007：`scripts/bench/**`、`docs/node-acceptance.md`

## 4. Constraints / forbidden

- **不 push、不开 PR、不 merge 到 master**；所有改动只合进本地的 `feat/macos-native`。
- **A 版不改**：`realtime_subtitle/translate/translator_queue.py` 一行都不能动。
- 安全底线（RFC 中 S1–S8 全部生效）：
  - 节点只绑定 Tailscale IP 和本机 UDS，**永远不绑 0.0.0.0**；
  - token 权限 0600，只通过 stdin 传递；
  - 日志里不写转录正文；
  - 不在磁盘上保存音频；
  - Ollama 只能通过 `ollama_url()` 访问，且必须经过 `_assert_local_ollama`。
- 不往 `config_local.py` 里加 `OLLAMA_BASE_URL`、`ALLOW_REMOTE_OLLAMA`，见 CLAUDE.md 第 2 节。
- mini2 上做远程操作（安装、停掉 v1、kill 进程做故障测试）：用户已经为 TK-007 批准，但**每一步都要先说清楚再执行**。
- SDD 流程：
  - 每个 TK 至少评审 2 轮；鉴权、生命周期、状态机相关的 TK（001、002、005）要准备第 3 轮。
  - 每次切换 phase 都要同步 CURSOR、TASKS、metrics。
  - 安全类和远程类的 TK 必须单独成批。
- 派活的路由：
  - codex 本机额度在 **2026-10-09 00:25 之前耗尽**；本机账号只认 `gpt-5.5`；`gpt-6.1-sol` 和 `gpt-5.3-codex` 都会返回 400。
  - codex 的沙箱写不了 worktree 的 gitdir，它只能把改动留在工作区，由 Coordinator 代为提交。
  - 给 codex 喂 stdin 时，**不要**在命令末尾再加 `</dev/null`。

## 5. Next steps（按顺序）

1. **回收 TK-001 和 TK-004**：
   - 检查有没有越界写入（TK-004 的 `main.swift`、`RemotePipeline.swift` 必须给出理由；可以放行最小的编译适配，或者移到 TK-005 处理）。
   - 运行验证命令。
   - TASKS 改为 reviewing，CURSOR 的 phase 改为 review，metrics 记一行。
2. **评审**（`self-healing-execution` 的 8 步闭环）：
   - 每个 TK 建一份 `reviews/CR-00N.md`。
   - 按 reviewer_lanes 派评审员（default，以及需要时的 security），至少 2 轮。
   - 由 Coordinator 分诊；接受的 finding 交给 Repairer 修，然后复查，直到收敛。
3. **交付**：合进 `feat/macos-native`（`git merge --no-edit feat/tk-00N`），跑全量 pytest、ruff 和 Swift release 构建，TASKS 改为 done，CURSOR 记交付摘要。
4. **下一批**：TK-002 单独一批 → TK-003 单独一批 → TK-005（依赖 TK-004）→ TK-006 → TK-007。
   - 每批都从 `feat/macos-native` 的 HEAD 开新 worktree：`git worktree add -b feat/tk-00N ~/projects/rs-tk-00N feat/macos-native`。
   - 执行者的派活 prompt 可以照抄本次 TK-001、TK-004 的模板：读卡片、RFC、CLAUDE.md，严格限定 write_scope，按格式回报。
5. **TK-007 验收**：
   - 先用 `install_node.sh mini2` 装 v2 节点，并停掉 v1（PID 34924）。
   - 再在本机用 `--local` 模拟第二个节点，测路由。
   - 功耗请用户执行 `sudo bash scripts/bench/power_compare.sh …`（需要先扩成五组）。
   - 结果写进 `docs/node-acceptance.md`。
6. 全部 TK 完成后执行 `/sprint-exit`，并更新记忆。

## 6. Verify（最小验证集）

```bash
cd ~/projects/rs-mac-native
QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest -q        # 当前基线：685 passed, 29 skipped
venv/bin/ruff check .
cd macos-native && swift build -c release --product rslite
.build/release/rslite --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless | grep -v volatile
```

## 7. Done criteria

以 RFC.md 中的「Done criteria」1–8 为准，摘要如下：
- 一键安装可重复执行，失败能回滚。
- 没有会话时不存在 worker 进程，gateway 常驻内存 <50MB，会话结束 120s 后回收。
- MacBook Air 自动进入混合档；mini2 断开时回退本机，字幕不断。
- 在 mini2 本机运行时走 UDS。
- 混合档：首字时间不慢于 B；最终 WER ≤ C+1pp；精修中位延迟 ≤4s；不重复、不丢句。
- 多节点：在静音点迁移、能回退，选择理由写进日志。
- 五组功耗都有数据。
- 测试和文档齐全。

## 8. Ready-to-paste prompt（给接手的 AI，另一个 Claude 账号或 Codex）

```text
你接手一个进行中的 SDD Sprint。先完整读这份交接包，再按里面的第 5 节「Next steps」继续：
  ~/projects/rs-mac-native/.governance/sprints-001-remote-offload/HANDOFF.md
（如果在 mini2 上看，路径是 ~/rs-HANDOFF.md，内容相同。）

然后读同一目录下的 CURSOR.md、TASKS.md、RFC.md，以及仓库根目录 CLAUDE.md 的第 4 节和第 7 节。
你的角色是 Coordinator，只做调度、评审和合并，业务代码交给子代理或 codex 写；codex 额度在 2026-10-09 00:25 之后才恢复。
契约 P1–P8 已冻结；不 push、不开 PR；在 mini2 上做远程操作前，先告诉用户你要做什么。
第一步：检查 ~/projects/rs-tk-001 和 ~/projects/rs-tk-004 两个 worktree 的 git status 和 git log，判断 TK-001、TK-004 进行到哪一步，再继续评审或重新派活。
回复用中文。
```
