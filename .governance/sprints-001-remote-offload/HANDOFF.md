# 交接包 — realtime_subtitle 外包算力版（sprints-001-remote-offload）

> 写于 2026-10-08 22:15 CEST，**最后更新 2026-10-08 22:45 CEST**，由 Claude（Coordinator）在额度可能耗尽前写成。接手的 AI 读完本文件，不需要再找用户补充背景。
> 本文件有三份副本：本机仓库 `~/projects/rs-mac-native/.governance/sprints-001-remote-offload/HANDOFF.md`、mini2 上的 `~/rs-HANDOFF.md`、本机记忆 `~/.claude/projects/-/memory/handoff-2026-10-08-rs-remote-offload.md`。

## 1. Goal

推进 SDD Sprint `sprints-001-remote-offload`，按队列完成 TK-001 到 TK-007，产品目标如下：
- rslite（macOS 原生字幕）在本机用省电小模型立即出字，也就是系统识别加系统翻译。
- 同时让最合适的节点（目前是 mini2，下一期加 Windows 主机）用大模型（Whisper turbo）对同一段音频做「整段精修」，按音频时间把本机那几句替换掉。
- 节点要能一键安装，平时不占资源，有会话时才加载模型，用完自动释放。

## 2. Context

| 项 | 值 |
|---|---|
| 主仓库 / 分支 | `~/projects/rs-mac-native`（git worktree），分支 `feat/macos-native`，HEAD 以 `git log -1` 为准，**从未 push** |
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
| TK-001 节点 worker | **reviewing：CR-002 已建，第 1 轮评审员尚未派出** | Claude sonnet 子代理（已完成）· `~/projects/rs-tk-001`，提交 **3e87063** | 在范围内。M2 真模型冒烟：WER **0.88%**，每段说完到出结果的延迟中位数 **3.25s**。下一步：并行派 default（opus）和 security（sonnet）两个评审员 |
| TK-004 rslite 时间+混合+单实例 | **fixing：CR-001 第 1 轮 needs_fix，6 条全部接受（F5 转给 TK-005）；修复者（sonnet）正在跑** | `~/projects/rs-tk-004`，提交 ecb7a20 + 0cdd5c7（coordinator-direct 编译适配） | 修复者完成后会在 feat/tk-004 上提交 `fix(native):`；若额度耗尽导致修复中断，先看 `git status`/`git log`，再按 `reviews/CR-001.md` 的 Round 1 表格继续修 F1–F4、F6，然后派第 2 轮复查（opus） |
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

1. **TK-004 / CR-001**：等修复者回报（或者自己检查 `~/projects/rs-tk-004` 的 git log）→ 越界检查、构建、headless 回放 → 派第 2 轮复查（opus，lens=default，只看 F1–F4、F6 有没有修好、有没有引入新问题）→ 收敛后 `git merge --no-edit feat/tk-004` 合进 feat/macos-native → TASKS 改成 done，REVIEWS 改成 resolved。
2. **TK-001 / CR-002**：并行派两个评审员（default 用 opus，security 用 sonnet；派活模板见 §8 的「工作方式」）。评审要核实执行者自己加的那几个细节（见 `reviews/CR-002.md` 第一段），以及 S1/S7；分诊、修复、第 2 轮复查（这个 TK 涉及生命周期，必要时做第 3 轮）；收敛后合并。
3. **下一批**：TK-002（gateway，安全类，单独一批）→ TK-003（install_node.sh，单独一批）→ TK-005（依赖 TK-004，**包括 CR-001 的 F5：在 main 里接上单实例，以及 `--selftest` 入口调用 `LineStore.selfTest()`**）→ TK-006 → TK-007（验收，远程操作单独一批）。每批都从 feat/macos-native 的 HEAD 开新的 worktree。
4. **TK-007 验收**：先用 `install_node.sh mini2` 装 v2 节点，并停掉 v1（PID 34924）；再在本机用 `--local` 模拟第二个节点测路由；功耗请用户执行 `sudo bash scripts/bench/power_compare.sh …`（需要先扩到 5 组）；结果写进 `docs/node-acceptance.md`。
5. 全部完成后执行 `/sprint-exit`，并更新记忆。

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

## 8. Ready-to-paste prompt（给接手的 AI：另一个 Claude 账号，或 00:25 之后的 Codex）

```text
你接手一个进行中的 SDD Sprint，按原 Coordinator 的工作方式继续开发，全程用中文回复。

【先读，按顺序】
1. ~/projects/rs-mac-native/.governance/sprints-001-remote-offload/HANDOFF.md（交接包。在 mini2 上读时，路径是 ~/rs-HANDOFF.md）
2. 同一目录下的 CURSOR.md、TASKS.md、REVIEWS.md、RFC.md、reviews/CR-*.md
3. 仓库根目录的 CLAUDE.md 第 4 节和第 7 节（踩过的坑），以及 ~/.claude/CLAUDE.md
4. 记忆：~/.claude/projects/-/memory/project_rs-macos-native-lowpower.md

【你的角色：Coordinator】
- 你只负责调度、分诊、合并和验收；业务代码交给子代理写。只有一两行、又必须依赖本会话上下文的改动，才由你自己直接改，并且单独提交，commit 标题标 [coordinator-direct]。
- RFC 里的契约 P1–P8 已经冻结，不许改。不 push、不开 PR。在 mini2 上做远程操作之前，先告诉用户你要做什么，用户已经批准过 TK-007。
- 不要杀用户正在运行的 rslite 或 main.py；每次跑之前先 pgrep。

【工作方式（照做）】
- 每个 TK 一个 worktree：git worktree add -b feat/tk-00N ~/projects/rs-tk-00N feat/macos-native
- Executor：Claude sonnet 子代理（在真机上跑，可以用 Speech/MLX/Translation）。codex 在 2026-10-09 00:25 之后恢复，只能用 `codex exec -c model=gpt-5.5 -c model_reasoning_effort=high -s workspace-write -C <worktree> - < packet`，不要在末尾加 </dev/null；codex 的沙箱不能 commit，要由你来代提交。不要派 agy。
- 派给 Executor 的 prompt 必须包含：TK 卡片路径、RFC 里对应的契约编号、write_scope 白名单、禁止修改的路径、必须实际跑的验证命令（pytest、ruff、swift build、headless 回放 ~/projects/rs-mac-native-data/concat5.wav）、回报格式（文件清单、验证原文、有没有越界、遗留风险），以及「不许声称跑过没跑的东西」。
- Reviewer：用一个和实现模型不同、更强的模型（实现用 sonnet，评审就用 opus）；涉及安全的 TK 另派一路 security 评审。评审员只读，按 id/severity/category/location/evidence/suggested_fix 格式输出，最后给 verdict，篇幅不超过 600 字。
- 每个 TK 至少评审 2 轮；涉及鉴权、生命周期或状态机的（TK-001/002/005），要准备第 3 轮。由你逐条分诊每个 finding：accepted 还是 rejected，并写明理由。Repairer 用 sonnet，只修 accepted 的 finding。
- 每次 phase 切换、每次派出或回收子代理，都要同步：TASKS.md 的状态、CURSOR.md（current_phase/tk/cr/round，以及「最近动作」时间线）、REVIEWS.md，并往 metrics/phase-trace.jsonl 追加一行 JSON；然后 git commit。
- 合并前跑一遍：pytest 全量、ruff、swift build -c release --product rslite。
- 每完成一步，就更新 HANDOFF.md（同时 scp 一份到 mini2 的 ~/rs-HANDOFF.md）和记忆文件，写清这次推翻了哪个假设、纠正了什么。

【第一步】
查看 ~/projects/rs-tk-004 和 ~/projects/rs-tk-001 两个 worktree 的 git status 和 git log，再按 HANDOFF 第 5 节继续：
(1) TK-004 等待或检查 CR-001 的修复结果，然后派第 2 轮复查；
(2) TK-001 派 CR-002 第 1 轮评审（default 用 opus、security 用 sonnet，两路并行）。
```

---

## 9. 云端接手须知（claude.ai/code 等 Linux 容器）

> 2026-10-08 23:00 用户要求推到 GitHub，在云端继续。以下分支已推送：`feat/macos-native`（主线，含全部 .governance）、`feat/tk-001`、`feat/tk-004`。

**云端做不了的事：**
- 编译不了 macOS/Swift（rslite），跑不了 MLX、Apple Speech、Apple Translation。
- 访问不到 mini2（Tailscale/ssh），也访问不到本机的 `~/projects/...` 和 `rs-mac-native-data`。
- 用不了子代理以外的本机 lane（codex、本机 venv）。

所以上文里所有 `~/projects/rs-…` 路径，在云端都要换成仓库内的相对路径，worktree 一律换成 **git 分支**。

**各 TK 在云端能做到什么程度：**

| TK | 云端可做？ | 怎么做 |
|---|---|---|
| CR-002（TK-001 评审） | ✅ | 读 `git diff 88d9355..origin/feat/tk-001` 做评审；pytest 中依赖 MLX、Speech 的用例会被 skip，其余可以跑（需要先 `pip install -r requirements-dev.txt numpy websockets`，再看 `faster_whisper` 能不能装上） |
| TK-002 gateway | ✅ | 用假 worker 测；`tailscale ip`、`ioreg`、`IOPS` 都放在可注入的接口后面，单测里 mock 掉 |
| TK-003 install_node.sh | ⚠️ 部分 | `bash -n`、shellcheck、`--dry-run` 能跑；真正装到 mini2 必须回到 Mac 上做 |
| TK-004 / CR-001 修复与复查 | ⚠️ 只能做代码评审 | `swift build` 必须在 Mac 上跑；复查结论标成「待 Mac 构建验证」 |
| TK-005 Swift 路由 | ⚠️ 可以写，但不能编译 | 写完必须回 Mac 跑 `swift build` 和 headless 冒烟，之后才能合并 |
| TK-006 文档 / 删 v1 | ✅ | |
| TK-007 验收 | ❌ | 必须在 Mac 和 mini2 上做 |

**云端的合并规则：**
- 在 GitHub 分支上工作，按 TK 开 `feat/tk-00N` 分支，从 `origin/feat/macos-native` 切出。
- 评审收敛后合进 `feat/macos-native` 并 push。
- **只有 Python 部分在云端跑过全量 pytest 的才允许合并；Swift TK 回 Mac 验证之后才能合并。**
- 不向 master 开 PR，也不合并到 master。

**TK-004 的状态：** 本机修复者推送时可能还没修完。以 `origin/feat/tk-004` 最新的提交为准，对照 `reviews/CR-001.md` Round 1 的表格，检查 F1–F4、F6 哪些已经修了。

### 云端版提示词

```text
你在云端接手 GitHub 仓库 wyl2607/realtime_subtitle 里进行中的 SDD Sprint，按原 Coordinator 的工作方式继续，全程用中文回复。
先 checkout 分支 feat/macos-native，按顺序读完：
1. .governance/sprints-001-remote-offload/HANDOFF.md（重点是第 9 节「云端接手须知」，它的优先级高于前文里的本机路径）
2. 同一目录下的 CURSOR.md、TASKS.md、REVIEWS.md、RFC.md、reviews/CR-*.md
3. CLAUDE.md 第 4 节和第 7 节

你的角色是 Coordinator：只负责调度、分诊、合并；业务代码交给子代理写。RFC 里的契约 P1–P8 已经冻结，不能改。不向 master 开 PR，也不合并到 master。

云端做不了 Swift、MLX、Apple Speech、mini2 相关的工作，凡是需要 Mac 的验证，一律标成「待 Mac 验证」，不要假装已经跑过。

工作流程：每个 TK 开一个分支；Executor 用 sonnet 子代理，Reviewer 用 opus，涉及安全的另加一路 security 评审。每个 TK 至少评审 2 轮，涉及鉴权、生命周期、状态机的要做第 3 轮。由你逐条分诊每个 finding：accepted 还是 rejected，写明理由。每次 phase 切换都要同步 TASKS、CURSOR、REVIEWS 和 metrics，然后 commit 并 push。

第一步：
(1) CR-002：评审 origin/feat/tk-001（diff 基线是 88d9355），default 用 opus、security 用 sonnet，两路并行；
(2) TK-004：检查 origin/feat/tk-004 上 CR-001 的修复进度，代码复查后标成「待 Mac 构建」；
(3) 接着做 TK-002 gateway（单独一批）。
```
