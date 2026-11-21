# Brief：rslite 当日字幕存档 + 简易历史窗（spec 003 P3 rslite 部分 / spec 010 C6）

协调者：Claude（2026-11-10）。实现派非 Claude lane。本文件是接口真相源。

## Goal

rslite（`macos-native/Sources/rslite`，Swift 原生 Mac 客户端）是四端里唯一没有「当日字幕存档」的。做两件事：

1. **`TranscriptArchive`**（新文件 `macos-native/Sources/rslite/TranscriptArchive.swift`，只 import Foundation，不碰 AppKit）：把上屏的句对写进每天一个文件，格式与 iOS `ios/RSiOS/TranscriptStore.swift` **逐列一致**（先读那个文件，照它的语义写）。
2. **`TranscriptWindow`**（新文件 `macos-native/Sources/rslite/TranscriptWindow.swift`，AppKit）：菜单栏菜单加一项「今天的字幕…」，打开一个普通窗口，显示今天存档、可搜索、可复制全部、可在访达中显示文件。

## 存档格式（与 iOS 相同，不许改）

- 目录：`$RSLITE_CONFIG_DIR/transcripts/`，没有该环境变量时 `~/.config/rslite/transcripts/`（与 `NodeRouter.swift` 约 291 行同一套规则；`RSLITE_CONFIG_DIR` 是冒烟隔离用的，必须尊重）。
- 文件：`YYYY-MM-DD.txt`（本地时区，`en_US_POSIX`）。
- 每行：`HH:mm:ss\t原文\t译文\t去重键\n`。没有译文时第三列为空串。老文件只有三列的行：读回时 key=nil，永不参与去重、永不被删除。
- 原文/译文里的 `\t` `\r` `\n` 一律替换成空格再写。
- 去重键：`"<session>#<lineKey>"`。`session` 是本进程启动时生成一次的代号（如 `String(UInt32.random(in: .min ... .max), radix: 16)` 加上启动秒数），因为 `Pipeline` 的句子 id 和 `LineStore` 的节点负数 key 每次启动都从头计数——只按 key 去重会让新会话覆盖旧会话的句子。
- 写法：内存按日期保存整天的行；任何一行内容真的变化时把**那一天**的文件整体原子重写（`.atomic`）；内容没变不写。首次触碰某一天时先把磁盘上的文件读回并重建 key→行索引（否则重启后会重复追加）。
- 同一 key 的后续更新改它原来那一行（哪怕那行在昨天的文件里），不追加。新 key 追加到**今天**的文件，时间列取追加时刻。

## TranscriptArchive 接口

```swift
final class TranscriptArchive {
    struct Line: Equatable { let time: String; let src: String; let dst: String }
    init(directory: URL? = nil, session: String? = nil, now: @escaping () -> Date = Date.init, timeZone: TimeZone = .current)
    static func defaultDirectory() -> URL
    func upsert(key: Int, src: String, dst: String?)   // dst nil → 空列
    func remove(keys: [Int])                            // 只删本 session 的这些 key 的行；找不到忽略
    func lines(for date: Date) -> [Line]                // 读回该天全部行，按文件顺序
    func fileURL(for date: Date) -> URL
    static func filter(_ lines: [Line], query: String) -> [Line]   // 空/全空白查询原样返回；原文或译文命中即留；.caseInsensitive + .diacriticInsensitive
    static func plainText(_ lines: [Line]) -> String    // 与 iOS plainText 相同：「时间␣␣原文」，有译文另起一行；句与句空一行
    static func selfTest() -> [String]                  // 返回失败描述，空 = 通过
}
```

线程：内部用一个串行 `DispatchQueue`，公开方法 `queue.sync`。写盘失败只记日志（`os.Logger`，**日志里不许出现原文/译文**），不抛。

`remove` 的用途：节点/云端精修句（P5）替换本机行时，被替换掉的本机行在屏幕上消失了，存档也要跟着删掉，否则同一时段会有两句。

## Overlay 接线（`Overlay.swift`，改动尽量小）

`Overlay` 持有 `private let archive = TranscriptArchive()` 和一个可选的 `TranscriptWindow`。存档内容 = `store`（`LineStore`）里出现过的行的**最后一版**：

- `addFinal`：`store.addLocal` 之后，**只有** `store.lines` 里真有这个 key 时才 `archive.upsert(key:src:dst:nil)`（`addLocal` 可能因为已被节点行覆盖而丢弃该行）。
- `addTranslation`：走到 `store.setTranslation` 的分支后，用 store 里该行的 src/dst 做 `upsert`。走 `pendingTranslation` 分支时不写（行还不在 store 里）。
- `addNodeFinal`：拿到 `store.applyNode(...)` 的返回值（被替换的 key 列表，目前被丢弃了），`archive.remove(keys: replaced)`；然后对新建的节点行（含随后 `setTranslation` 补上的 pending 译文）`upsert`。
- 「未翻译 ⟳」失败标记**不进存档**（它不是译文）；`markUntranslated` 及 `failures` 相关路径不要碰存档。
- 存档变化后若历史窗开着，调用它的 `reload()`。
- 菜单：在 `statusMenu` 里「设置…」那一项**之前**加 `NSMenuItem(title: "今天的字幕…", action: #selector(openTranscript), keyEquivalent: "")`，`target = self`。不要占 ⌘H。

Headless 模式（`RSLite.swift` 的 `HeadlessLineStore`）不接存档，不改。

## TranscriptWindow（AppKit，保持简单）

- 普通 `NSWindow`（可缩放、可关闭，标题「今天的字幕」，约 560×640），关闭只隐藏（`isReleasedWhenClosed = false`），再次打开复用并 `reload()`。
- 顶部：`NSSearchField`（输入即过滤，调 `TranscriptArchive.filter`）、按钮「复制全部」（把**当前过滤后**的 `plainText` 放进 `NSPasteboard.general`）、按钮「在访达中显示」（`NSWorkspace.shared.activateFileViewerSelecting([fileURL])`；文件不存在就打开目录，目录也不存在就先建）。
- 主体：只读、可选中复制的 `NSTextView`（放在 `NSScrollView` 里），内容 = 过滤后的 `plainText`；空时显示「今天还没有字幕」。
- `reload()` 重新 `archive.lines(for: Date())` 并按当前搜索词刷新；若用户没在往上翻（滚动条在底部），刷新后滚到底。
- 文案就这几句，写在这个文件里即可（它们不是状态文案，`design/tokens.json` 不用动）。

## 自测（必须）

`TranscriptArchive.selfTest()` 用 `FileManager.default.temporaryDirectory` 下的新随机目录，结束删掉，至少覆盖：
1. upsert 新 key → 文件一行、四列、第四列是 `<session>#<key>`；dst nil → 第三列空。
2. 同 key 再 upsert（补译文）→ 仍一行，内容是新版。内容相同的 upsert 不重写文件（可用文件修改时间或内部写计数判断，二选一）。
3. `remove` 删掉本 session 的行；不同 session 同 key 的行不受影响。
4. 新建一个**同 session** 的实例读同一目录 → upsert 同 key 改原行而不追加（索引从文件重建）；**不同 session** 的实例 upsert 同 key → 追加新行。
5. 老三列行读回保留、不被去重、不被 remove。
6. `\t` `\n` 被替换成空格。
7. 注入 `now`：昨天写入的 key 今天更新 → 改昨天文件；今天的新 key 进今天文件。
8. `filter`：空查询原样；`"uber"` 能命中 `"Über"`；只命中译文的也留下。`plainText` 格式与 iOS 相同（含无译文的情况）。

在 `RSLite.swift` 的 `runSelfTest()` 里、`FileSource` 那一行之后加：
`failures.append(contentsOf: TranscriptArchive.selfTest().map { "TranscriptArchive: \($0)" })`

## Constraints（硬性）

- 只允许改/建：`macos-native/Sources/rslite/TranscriptArchive.swift`、`macos-native/Sources/rslite/TranscriptWindow.swift`、`macos-native/Sources/rslite/Overlay.swift`、`macos-native/Sources/rslite/RSLite.swift`、`macos-native/README.md`（可选：加一小段说明存档位置）、`LANE_REPORT.md`。别的文件一律不动。
- ☠️ CI 用 Xcode 26.6（Swift 6.3），本机更新：**闭包里引用实例字段一律写 `self.`**；不要在闭包里引用函数后面才声明的同名局部变量。否则 CI 报 circular reference 或 release 构建卡死，本地不报。
- 不加设置项、不加保留期清理、不做 SRT 导出（存档里没有音频时间）、不改 `LineStore`/`Pipeline`/路由。
- 测试样本自己现写（德语/中文短句随便编）。
- 不 commit（协调者会复核后提交）。

## Done（门禁，全部要过）

```bash
swift build -c release --package-path macos-native
macos-native/.build/release/rslite --selftest   # 最后一行必须是 SELFTEST OK
python3 scripts/design/gen_tokens.py --check
python3 scripts/design/check_copy_literals.py
python3 scripts/design/check_parity.py
```

完成后写 `LANE_REPORT.md`：改了哪些文件、每个接线点怎么做的、自测覆盖了哪些情况、门禁实际输出的最后几行。
