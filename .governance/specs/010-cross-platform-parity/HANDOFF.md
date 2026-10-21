# 接手包 — spec 010 四端统一体验（2026-10-21 20:15 暂停）

> 写给下一个接手的人或 AI。仓库内同一份在 `.governance/specs/010-cross-platform-parity/HANDOFF.md`（分支 `lane/rs010-int3`）。
> 一句话：**四端代码层面基本拉齐**（同一套路由规则、句对替换规则、状态文案、设计令牌），**两个手机端都还没上过真机**；暂停时有 4 条修复线刚开始、已全部停下。

---

## 1. 四个平台现在到哪了

| 平台 | 已发布 | 开发中 | 估计进度 | 还欠着 |
|---|---|---|---|---|
| Windows | v3.1.0 | v3.2.0 | ~85% | **P1 未修：桌面端 5 秒就放弃节点**（见 §4）；重装 Windows 节点要用户同意；用户真机项（冒烟、高缩放屏） |
| Mac（Python + rslite） | v3.1.0（v3.2.0-beta tag） | v3.2.0 | ~85–90% | 同上 P1；**F4 未修：rslite 换 key 时旧 401 把新 key 打成无效**；拔网回退、悬浮窗目测 |
| Android（私有仓库） | v0.4.0-quality（旧架构） | rewrite/v1 | 代码 ~80%，真机 0% | **A5 未修：节点握手前无超时（回归，严重）+ 换 key 迟到 401**；从没装到一加 13 |
| iOS | 未发布（spike） | 首个版本 | 代码 ~65%，真机 0% | **I5 未修：5 个问题**（见 §4）；从没在 iPhone 上跑过 |

百分比是协调者估的，不是仓库里记录的数字。

## 2. 代码在哪（全部已推到 GitHub，**没有合 master/main**）

**主仓库 `wyl2607/realtime_subtitle`**
- **最新复核通过的集成点：`lane/rs010-int3` @ `c6f91e5`** —— 从这里接着做。含：spec 010 规格、v3.2.0 收尾（云端文案令牌化、429 冷却后缀、FLEURS 入口）、iOS 统一（路由/P5/自动回退/存档/云端核心/云端接线/loading）、节点冷启动 loading 协议、Windows CRLF 修复、四端「401 后迟到 429」规则修复、rslite 冷却后缀上屏修复、三语 README 进展表。
- 复核结果（int3，I3 合入前）：Windows 真机 pytest **1218 过 0 失败**；Mac pytest **1449 过 0 失败**、ruff 绿；mini2 上 rslite --selftest、iOS 三个自测入口、模拟器构建、`check_parity.py` 全过。I3 是快进合入，I3 自身在 mini2 上复核通过（iOS 自测 + 构建 + PARITY OK）。
- 各单线分支（都已并进 int3）：`lane/v320-closeout` 9281156、`lane/f1-rules` a75714c、`lane/f2-mac-status` d8bc882、`lane/d1-docs` 2474d10、`lane/i1-ios-cloud-core` 3873581、`lane/i2-ios-fixes` 380346c、`lane/i3-ios-cloud-wire` c6f91e5、`lane/i4-ios-node-loading` 22a42bd。

**Android 仓库 `wyl2607/realtime_subtitle_android`（私有）**
- **最新复核通过：`lane/f3-android-rules` @ `0443f8b`**（链：`rewrite/v1` 26907ae → `lane/android-cloud` c33c072 → `lane/r2-android-cloud-ui` 26cde21 → `lane/r3-android-node-loading` 9aceaef → `lane/f3-android-rules`）。
- 复核结果：`--rerun-tasks` 395 个测试 0 失败，assembleDebug 出 APK。
- ⚠️ `lane/r3-android-node-loading` 引入了**握手前无超时的回归**（§4 A5），f3 继承了它——别直接拿 f3 发版。

## 3. 暂停时在跑的 4 条线（都停在第 1 次尝试中途）

任务卡都在仓库 `.governance/specs/010-cross-platform-parity/lanes/`（也在 MBA `~/lanes/briefs/rs010/`）。

| 线 | 机器 | 基线 | 半成品 | 内容 |
|---|---|---|---|---|
| **A5** android-fixes | MBA | Android `lane/f3-android-rules` 0443f8b | **有**：3 个文件，补丁 `MBA:~/lanes/briefs/rs010/paused/a5-android-fixes.partial.patch`（5.5KB，未复核） | ① NodeClient `awaitReady()` 开头的 `select { helloSent / ready }` 没超时（旧版有 `withTimeout(readyTimeoutMs)`）→ 节点握手挂住时会话永远不返回、不降级本机；② 换 key 时旧 client 的迟到 401 把新 key 永久打成无效 |
| **F4** rslite-keyrot | MBA | `lane/rs010-int3`（工作区在 6734ece） | 无 | `CloudSegmenter.swift` `refreshSwitchLocked()` 的 `key_modified` 分支不作废代次、不取消旧请求。**坑**：函数末尾的 `if cancel { client?.cancelInflightRequests() }` 在换 client 之后执行，不能只把 cancel 置真——会取消新 client |
| **P1** py-node-connect | MBA | `lane/rs010-int3`（工作区在 6734ece） | 无 | `app.py` 约 697 行 `_run_on_loop(client.connect())` 用默认 `timeout=5` → 桌面端 5 秒放弃节点，15s/60s 等待全不可达（已确认）；另两条待核实：gateway 复用未 ready 的 worker 不发 loading、连接后状态行短暂显示错节点名 |
| **I5** ios-fixes2 | mini2 | `lane/i3-ios-cloud-wire` c6f91e5 | 无 | 切回节点截断正在说的句子；译文按 id 回填写错行（跨会话 id 重复）；广播结束竞态复活本机；换 key 迟到 401；（待核实）分段器大缺口后 flush 出空段 |

**注意**：F4 和 P1 的工作区停在 6734ece（int3 合入 I3 之前），重开时先 `git -C <工作区> merge --ff-only lane/rs010-int3` 或直接基于 int3 重建。

**恢复一条线**（以 MBA 为例；mini2 用 `~/rs-lanes/bin/lane-chain.sh` 并把门禁包进 `~/rs-lanes/bin/gate.sh '<命令>'`）：
```bash
# 工作区必须干净才能起；A5 要先决定用不用半成品补丁（git apply 后自己复核，或丢掉重来）
nohup env GATE_CMD="<见卡片「验证命令」>" ALLOW="<卡片 write_scope，空格分隔>" REQUIRE_CHANGE=1 GATE_ALLOW_MISSING=1 \
  bash ~/lanes/bin/lane-chain.sh <工作区> <卡片.md> LANE_REPORT.md \
  oc:opencode/deepseek-v4.1-flash-free oc:openrouter/minimax/minimax-m3.1-flash:free \
  oc:nvidia/deepseek-ai/deepseek-v4.1-flash oc:opencode/glm-5.3-flash-free > <日志> 2>&1 &
```
现成的写法可以照抄 `MBA:~/lanes/briefs/rs010/launch-f.sh`、`launch-i.sh`。

## 4. 已确认、还没修的问题（不要以为已经修了）

| # | 平台 | 问题 | 归属 |
|---|---|---|---|
| 1 | Windows/Mac 桌面 | 节点连接 5 秒就放弃（`_run_on_loop` 默认 timeout=5） | P1 |
| 2 | Android | 节点握手前无超时（回归） | A5 |
| 3 | Android | 换 key 迟到 401 把新 key 打成无效 | A5 |
| 4 | Mac rslite | 同上（换 key 迟到 401） | F4 |
| 5 | iOS | 切回节点截断下一句 / 译文写错行 / 广播结束竞态 / 换 key 迟到 401 | I5 |
| 6 | Python 桌面 | 运行中直接替换 `groq.key` 文件（不经设置面板）时，换 key 迟到 401 同样会出问题——非支持路径，暂只记录 | 未派 |

评审原文都在 `MBA:~/lanes/briefs/rs010/reports/REVIEW-*.md`（含协调者核对结论），每条 lane 的报告在同目录 `<线名>.md`。

## 5. 等用户决定 / 只能用户做

1. **额度规则**：「日请求数只升不降」是 spec 002 的有意设计（Python 注释写明），代价是服务端每日重置后可能多锁最长 24h。改不改、四端一起改？（Android 评审提出，未改）
2. **真机**：一加 13 联网 + adb（MBA USB 或 Tailscale 无线调试）；iPhone（免费签名 7 天重签，付费开发者账号到了才能 TestFlight）。
3. **重装 Windows 节点**（yzwdmsnlmm，让冷启动 loading 生效）——按发布清单要先问用户。
4. v3.2.0 发布清单里的用户在场项：拔网、悬浮窗目测、Windows 冒烟、高缩放屏。
5. 合 master / 打 tag / 发 Release：每一步都要用户点头。

## 6. 基础设施与规矩（踩过的坑都在这）

**机器**
- **MacBook Air**（`ssh yumei@macbook-air`）：主构建机（Swift/Xcode/Gradle/pytest），lane 在 `~/lanes/wt/`，派活脚本 `~/lanes/bin/lane-chain.sh`。8 核，build 型 lane 最多同时 3 条。`~/lanes/wt` 已放 `.metadata_never_index` 关掉 Spotlight（之前 mds_stores 吃掉 94% CPU）。
- **mini2**（经 MBA `ssh mini2`）：用户 10-21 批准当第二台 iOS/Swift 构建机。lane 只在 `~/rs-lanes/`；**不准碰** `~/rs-node`、LaunchAgent `com.realtimesubtitle.node`（PID 95865）及其端口。磁盘只剩 ~15GB：门禁必须包 `~/rs-lanes/bin/gate.sh`（剩余 <5GB 拒绝构建、跑完必删构建产物）。
- **coco**（经 MBA `ssh coco`，Linux 4 核）：只跑**公开仓库**的文档 lane 和评审，在 `~/rsrev/`；它的 `/tmp/lane-chain` 属于别的用户，lane-chain 副本已改成日志写 `~/rsrev/lane-chain-logs`。`/home/coco` 下有一个不是我们的长期 opencode 进程，别碰。
- **这台 Windows**：Windows 真机 pytest 在 `~/rs-test010`（git worktree）跑：`PYTHONPATH=~/rs-test010/.testdeps`（生产 venv 缺 websockets/jiwer/opencc，装在隔离目录，**不往生产 venv 装东西**）+ `--basetemp=.pytest-tmp`（共享临时目录有权限问题）+ PATH 加 scoop ffmpeg。

**模型**
- 只用免费档：`opencode/deepseek-v4.1-flash-free`、`openrouter/minimax/minimax-m3.1-flash:free`、`nvidia/deepseek-ai/deepseek-v4.1-flash`、`opencode/glm-5.3-flash-free`。
- **禁用 `deepseek-v4-pro`（用户：太贵）**；Go 订阅周额度已用 ~70%，新线不用 Go。
- 用户已同意免费模型读写 Android 私有仓库；但**自动模式会拦截把私有仓库拷到别的服务器**（coco）——Android 一律在 MBA 上跑。
- 多个 opencode 同一秒启动会撞 `database is locked`：错开 15–25 秒。

**门禁与复核（最重要的经验）**
- **模型自报的「通过」不算数**：协调者每条都自己重跑（Gradle 要 `--rerun-tasks`，否则全是 UP-TO-DATE 假象）。
- **测试绿 ≠ 没回归**：A5 的握手无超时就是测试全绿漏过去的。动到超时/并发/生命周期的线，进集成分支前必须单独评审。
- 免费模型评审很有价值，但每条发现都要打开代码核对（这轮约 20 条发现里绝大多数成立，个别是针对旧基线的）。
- 写门禁前先在**干净基线上跑一遍**确认能过：这轮因为协调者自己的门禁错误白跑了好几次——基线夹具副本没同步导致 DRIFT、tokens.json 末尾追加键必然改上一行逗号（用结构比对 `~/lanes/bin/check_tokens_additive.py`，别用「diff 里没有 - 行」）、禁删规则误伤注释（只匹配代码形态如 `TailnetPin\.`）、write_scope 漏了必须改的自测文件。
- Mac 上 pytest 必须 `TMPDIR=/tmp`（否则 AF_UNIX path too long）；Android `JAVA_HOME=/opt/homebrew/opt/openjdk@21 sh ./gradlew`；iOS 要 PATH 带 `/opt/homebrew/bin`（xcodegen）。
- 杀 lane 要按进程树精确杀（`~/lanes/bin/pause_lanes.sh` 会先存补丁再杀）；`pkill -f` 模式写宽了会误杀别的线。

**推送**
- MBA/mini2/coco 都没有 GitHub 凭据：`bash C:\Users\yzwdm\rs-push\push_lanes.sh`（在 Windows 上跑）从 MBA 打 bundle 再推。mini2/coco 的分支要先 bundle 到 MBA 的仓库里。只推 `lane/*`，**不推 master/main，不 force**。
- **提交身份必须是 `yumei <yumei@localhost>`**（各机器 repo 级已设）：mini2 全局 git 邮箱是用户的私人 hotmail，GitHub 会以 GH007 拒推，而且本来就不该公开。

**跨机器盯 lane**：`bash C:\Users\yzwdm\rs-push\watch_lanes.sh`（任一 lane 出结果就退出）。**注意**：重置 `seen.txt` 会把已完成但还没读的评审标成已读——这轮因此晚读了两份评审。

## 7. 建议的接手顺序

1. 先把 **P1**（桌面端 5 秒放弃节点）和 **A5 ①**（Android 握手无超时）修掉——这两个直接让节点功能在一端失效/挂死。
2. 再 F4、A5 ②、I5。
3. 新代码合进 int3 后，三台机器各跑一遍全量（Windows pytest、Mac pytest + ruff + rslite selftest、mini2 iOS 自测 + 构建 + check_parity）。
4. 推动真机：Android 装 APK 到一加 13；iOS 免费签名装 iPhone。
5. v3.2.0 发布清单剩余项 → 用户点头后合 master、打 tag。
