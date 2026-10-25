# `lane/rs010-int3` 合回 master 的方案（待用户定）

> 2026-10-25 盘点。只出方案，**本 PR 不动 master 上的任何代码**；选哪种、何时合，由用户决定。
> 所有数字都是 `git fetch` 之后对 `origin/master`（398b128）和 `origin/lane/rs010-int3`（f11aa05）量的。

## 1. 现状

| 项 | 值 |
|---|---|
| int3 比 master 多 | **232 个提交**（71 个 merge + 161 个普通提交），时间 10-11 → 10-22 |
| master 比 int3 多 | 1 个：398b128（#78，只改 `.governance/sprints-001-remote-offload/HANDOFF.md` 和 `docs/TODO.md`） |
| merge-base | 23b8131（#75，10-19）——int3 在 10-19 已经把当时的 master 合进来过（ad4fe98） |
| 净差异 | 259 个文件，+51030 / −867 |
| 与 master 的文本冲突 | **0**（`git merge-tree --write-tree origin/master origin/lane/rs010-int3` 干净） |
| 已被 int3 完整包含的分支 | `feat/macos-native`、`lane/rs010-integrate`、`lane/rs010-ios-integrate`（三者相对 int3 都是 0 ahead） |
| v3.2.0-beta tag | 69c9604，**在 int3 这条线上、不在 master 上** |
| 版本号 | master `version.py` = 3.1.0；int3 = 3.2.0 |

**squash 重复的核查**：master 是 squash 仓，所以没信 `git cherry`，而是看 merge-base 之后两边各自改了什么。
merge-base 之后 master 只多了 #78 那两份文档，int3 一个都没碰；merge-base 之前的 master 内容 int3 已在
ad4fe98 合进来过。结论：**int3 里没有"已经被 squash 进 master、会被合第二遍"的代码**，上面 259 个文件
全是 master 上没有的新内容。

**这件事卡着 v3.2.0 正式版**：用户是 `git pull` master 拿更新的（CLAUDE.md 第 3 节），而 v3.2.0
的全部内容（云端识别、Windows 接节点、多层路由、`requirements.txt` 新增 websockets/opencc）只在 int3 上。
不合回 master，正式版就发不出去。

## 2. 内容按主题/风险分组

161 个普通提交里约 40% 同时改了两个以上区域（例如 TK-002 一个提交同时动 Python 路由、Swift 路由和共享夹具），
所以**按提交拆不干净，按路径拆才拆得开**。按路径统计（相对 merge-base）：

| 组 | 路径 | 规模 | 影响谁 | 风险 |
|---|---|---|---|---|
| G1 Windows/桌面 Python 核心 | `realtime_subtitle/app.py`(+1068)、`translate/translator_queue.py`(+490)、`ui/*`(settings_window +694、subtitle_render +267…)、`asr/*`、`config.py`、`requirements.txt`、`scripts/windows/bootstrap.*` | 30 文件 +7.2k | **所有 Windows 用户** | **高**：这是 v3.2.0 发给老用户的部分，只能靠 Windows 真机兜底 |
| G2 远端路由/云端/节点（Python） | `realtime_subtitle/remote/`（新，2.7k）、`realtime_subtitle/node/`(+1.5k)、`scripts/node/*` | 默认关闭 | 开了节点/云端开关的人 | 中：默认关；没配 key、节点开关关时行为同 3.1.0 |
| G3 rslite（Mac 原生） | `macos-native/`（+11.4k） | 24 文件 | Mac rslite 用户 | 中：CI `rslite-macos` 目前带 `continue-on-error`，在 Swift 6.3 上编不过（#80 在修） |
| G4 iOS | `ios/`（新子树，+9.4k）、CI `ios` job | 49 文件 | 无现有用户 | 低：全新子树，不影响桌面 |
| G5 共享契约 | `tests/fixtures/route_cases.json`、`design/tokens.json` + 生成物 | 小 | G1–G4 都读 | **耦合点**：Python/Swift/iOS 读同一份夹具和令牌，拆 PR 时必须跟最先合的那个走 |
| G6 治理/文档 | `.governance/`（69 文件 +8.1k：sprint-002、spec 003/007/008/009/010、v3.2.0 发布清单）、`docs/`、三语 README、CLAUDE.md | 文档 | — | 低，但量大，会让 PR diff 难读 |
| G7 测试/CI | `tests/`（49 文件 +10k）、`.github/workflows/ci.yml`（新增 rslite-macos、ios job，pipeline-helpers 补 opencc） | — | — | 中：CI 改动跟着 G1/G3/G4 |

## 3. 三种合回策略

### 方案 A：整体一个 PR，用 **merge commit** 合（不 squash）

int3 → master 开一个 PR，GitHub 选 "Create a merge commit"（仓库设置允许）。

- **冲突量**：0（已用 merge-tree 验证）。
- **CI 风险**：PR 的 CI 就是 int3 tip 的 CI。目前已知唯一的红是 `rslite-macos`（Swift 6.3），它带
  `continue-on-error`，不挡合并；#80 合进 int3 后可以去掉这个豁免。int3 tip 本身没有 push 触发的 CI 记录，
  最近一次全套 CI 是 #80（base = int3 的 3336dd0）：除 rslite-macos 外全绿。
- **对 v3.2.0 的影响**：最直接——PR head 就是要发的东西，Windows 真机验收按发布清单
  （int3 上的 `.governance/release-checklist-v3.2.0.md`）对着这个 head 跑，过了合、合了在 master 上打 `v3.2.0`。
  v3.2.0-beta tag 也自然成为 master 的祖先，`git log v3.2.0-beta..v3.2.0` 能用。
- **代价**：master 历史里多进 232 个提交、71 个 merge（master 以前就有 `Merge lane/...` 这类 merge 提交，不算破例）；
  5 万行的 PR 没法逐行审，审查只能靠"int3 每个 lane 合入时已审过 + 真机验收"。
- ☠️ **不能 squash**：squash 会切断祖先关系，之后所有从 int3 拉出来的 lane（`~/lanes/wt/` 下约 30 个 worktree）
  对 master 全部"看起来分叉"，正是这次盘点 `git cherry` 不可信的根源，会再来一遍。

### 方案 B：按主题拆成 4 个 squash PR

按第 2 节的路径分组，从 int3 tip 按路径 checkout 出来（不 cherry-pick 提交——四成提交跨区，摘不干净）：

1. **PR-1 桌面 Python 核心 + 远端/节点 + 共享契约 + 对应测试/CI**（G1+G2+G5+G7 的 Python 部分）——v3.2.0 本体
2. **PR-2 rslite**（G3 + 设计令牌生成物 + bench + rslite-macos job），先合 #80
3. **PR-3 iOS**（G4 + ios job）
4. **PR-4 治理/文档**（G6）

- **冲突量**：每个 PR 对 master 文本冲突为 0，但 PR-1 合完之后 PR-2/3 之间仍可能在 `ci.yml`、`CLAUDE.md`、
  README 这几个共享文件上互相冲突（小，手工可解）。
- **CI 风险**：**中到高**。int3 只在"全部一起"的状态下被测过；拆出来的中间态（例如 PR-1 合了、iOS 还没合时
  `tests/` 里读 iOS 夹具的跨端一致性用例）从没跑过，需要逐个 PR 修 CI。共享夹具/令牌必须跟 PR-1 走，
  否则 Python 测试先红。这正是 CLAUDE.md 第 4 节第 26/28 条"单看每一处都对"那一类风险。
- **对 v3.2.0 的影响**：PR-1 合完即可发 v3.2.0（Windows 真机只需验 PR-1），审查面比 A 小很多；
  但 Windows 真机验的是"拆出来的 PR-1"，不是 int3 上已验过的那个组合，前面 10-19 的两轮验收结果要重跑。
- **代价**：拆分本身要 1–2 天 lane 工作量；合完后 int3 与 master 失去祖先关系，int3 必须退役、所有在飞 lane
  （#80、#81、#77、`quota-reset-port`）要重新基于 master 开。

### 方案 C：先把 int3 线性化 rebase 到 master，再快进/merge

- **冲突量**：对 master 是 0，但 rebase 要把 71 个 merge 拍平，int3 内部各 lane 之间当时解过的冲突
  （例如 ad4fe98 的 `requirements.txt` opencc/torch 冲突）要重解一遍，工作量和出错面都最大。
- **CI 风险**：高。232 个改写过的提交里每一个中间点都没测过；`git bisect` 好看，但只有 tip 可信。
- **对 v3.2.0 的影响**：所有 SHA 改写，v3.2.0-beta tag 不再指向线上的任何提交；约 30 个 lane worktree
  全部失效需要重建；拖慢正式版。
- **代价**：最高，收益只有"历史好看"。**不推荐**。

## 4. 推荐：方案 A（merge commit 整体合回），按下面的顺序

1. **先把 int3 收口**：#80（Swift 6.3 兼容）合进 int3，去掉 `rslite-macos` 的 `continue-on-error`；
   #81（iOS 麦克风模式，草稿）和 `quota-reset-port`（#77 的移植）**不等**——它们没进 int3 就不在这次合回范围里，
   之后基于 master 继续。
2. 冻结 int3（之后的 lane 改基于 master 开），从 int3 tip 开 int3 → master 的 PR，CI 全绿。
3. 对**这个 PR 的 head** 跑 Windows 真机 + groq key 那几项（发布清单里还没过的 FLEURS-60 等）。
4. 用户合并（Create a merge commit），在 master 上改 `__version_date__` 后打 `v3.2.0`。
5. 合完 int3 退役；`~/lanes/wt/` 下基于 int3 的 worktree 按需重建到 master。

理由一句话：**零冲突、与 master 没有 squash 重复、int3 tip 正好就是 v3.2.0 要发的组合**；拆分和 rebase
都是在付出额外工作量的同时，把"已经在真机上验过的组合"换成"没人测过的新组合"。

方案 B 只在一种情况下更好：用户想在 v3.2.0 只发 Windows 部分、明确不想让 rslite/iOS/治理文档进 master。

## 5. 不在本 PR 范围内

- 本 PR 只加这一份文档，不合并、不发版、不改 int3。
- 相关在飞 PR 的状态见 PR 描述。
