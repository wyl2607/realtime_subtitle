# Spec 003：字幕界面个性化与体验（Windows PyQt6 + Mac rslite）

来源：用户 2026-10-11 真机直播验收中提出（看不清、要能改颜色/字号/透明度/形状、历史与失败句、译文出现体验）。
分析依据：`analysis.md`（agy 只读分析，协调者已抽查 B 节两条：只显示译文时失败句隐形 `subtitle_render.py:138-146`、`_trim_tx_queue_locked` 静默丢句 `translator_queue.py:897`，均属实）。

## Goal
两端字幕「看得清、可定制、不丢句、不乱跳」。

## 已完成（不在本 spec 范围）
- rslite 深色底 + 描边阴影（e8db421）、字号 28（本日）。
- Windows 配色/按钮统一（lane/rs-ui-win，进行中）。

## 分期（每期独立可发）
| 期 | 内容 | 两端 | 依赖 |
|---|---|---|---|
| P1 字幕样式 | 像视频网站字幕设置：字号、原文色、译文色、背景色、背景透明度、文字边缘（无/阴影/描边）、窗口形状（圆角方框/贴底横条）、显示行数、显示模式（双语/仅译文/仅原文）、首选节点。设置即时生效并持久化（Windows: window_state.json tuning；rslite: 新增 JSON 配置文件，菜单「字幕样式…」） | 两端 | rs-ui-win、TK-009B 合并后 |
| P2 失败句 | 句子状态机 PENDING→TRANSLATING→SUCCESS/FAILED_RETRYING→FAILED_TERMINAL；失败句显示「未翻译 ⟳」（仅译文模式回退显示原文），后台自动重试 1 次、点击手动重试；队列积压丢弃的句子也进入 FAILED 而不是蒸发；rslite Apple 翻译失败同样处理 | 两端 | P1 |
| P3 历史 | 统一 SubtitleHistoryStore（key/t0/t1/src/dst/source/status），主窗/历史弹窗/电视窗/影院条订阅同一数据源，P5 替换与删行全部同步；历史弹窗按时间排序、显示来源、搜索、复制/导出 SRT；语言切换插分界线 | Windows 为主，rslite 加简易历史 | P2 |
| P4 出现体验 | 先量化（草稿首字延迟、正式译文延迟、每句驻留时长、重排次数），再做：原位替换不整窗重排、新句淡入、最短显示时长、最新句高亮；rslite 按 key diff 更新视图不清空重建 | 两端 | P3（Windows 渲染重构风险 L） |

## Constraints
- 不改识别/翻译/路由行为；模式系统仍只走 `_apply_mode`；命中测试 BTN_RESERVE/WM_NCHITTEST 不动。
- 新增持久化项若是像素单位，加入 `rescale_state_for_dpr` 名单（CLAUDE.md 第 43 条）。
- 每期两端各一条 lane，写入范围不重叠；协调者出离屏渲染前后对比图验收。

## Done（每期）
pytest 全绿、ruff 绿、`rslite --selftest` OK；离屏/真机截图前后对比；Windows 真机（yzwdmsnlmm）看一遍。
