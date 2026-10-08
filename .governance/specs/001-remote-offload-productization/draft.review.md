# 001 Draft Review

- 日期：2026-10-08；评审对象：draft.md @147e561
- 架构评审：codex gpt-5.5（只读），verdict needs_fix
- 安全评审：Claude 子代理（general-purpose），verdict needs_fix

## A. lens=architecture

Findings：

- id: F01；severity: major；category: 行为正确性/协议v2；location: draft.md:75-83，Pipeline.swift:24-30,111-118；evidence: 草案依赖 B 侧 `audioTimeRange` 做替换，但现有回调只有 `id,text`，且 `attributeOptions` 为空；未定义带时间的句子模型和失败口径。suggested_fix: 明确 `PipelineCallbacks`/Overlay 的 `[t0,t1,source]` 契约和对应测试。

- id: F02；severity: major；category: 恢复路径/生命周期；location: draft.md:96-100；evidence: 只描述“更强节点迁移”的 drain 流程，未定义当前节点断线/强杀时如何标记离线、关闭旧会话、接入次优节点或本机，Done criteria 要求 mini2 关闭字幕不中断。suggested_fix: 补充 active session 断线状态机和回退验收用例。

- id: F03；severity: major；category: 状态落盘/配置假设；location: draft.md:63-65,89-90,208；evidence: `rtf` 会滑动更新、路由依赖节点身份，但 `nodes.json` 只含 `{id,url,token_file}`，Tailscale IP/MagicDNS 还未定，未说明能力/rtf/失败状态如何原子落盘。suggested_fix: 定义节点配置与运行状态文件 schema、权限、原子写和迁移规则。

- id: F04；severity: major；category: 告警与回滚；location: draft.md:111-120,196；evidence: 安装会停掉并删除旧 `~/rs-remote`，但没有备份、失败回滚或 launchd 启动失败后的恢复路径。suggested_fix: 安装方案加入 preflight、备份、回滚步骤和失败告警。

- id: F05；severity: minor；category: 可测试性/Done criteria；location: draft.md:184, requirement.md:82，scripts/bench/power_compare.sh:2,32-35；evidence: 需求要求空闲/B/C/A/混合五组功耗，现有脚本只测 idle/local/remote，草案未列脚本改造。suggested_fix: 把五组测量和结果文档写进影响面与验收。

verdict: needs_fix
## B. lens=security

- S1 major｜配置假设｜§1.2、§3.1 engines.py；CLAUDE.md §7 第29条
  - 证据：节点 worker 退回 Ollama 时，草案没写要复用 `_assert_local_ollama()` 的环回校验，新路径会绕开全项目唯一的硬失败保护。
  - 修复：在 Translator 的 Ollama 适配里强制调用这条校验，并补测试。
- S2 major｜配置假设｜§1.3、§1.8-4；server.py:36
  - 证据：Tailscale IP 写死在 plist 里。开机时 tailscaled 还没起来、或者 IP 变了，bind 就会失败，KeepAlive 会进入崩溃循环；草案也没写继续禁止 0.0.0.0。
  - 修复：gateway 启动时调用 `validate_host`；bind 失败就退避重试并告警，绝不退回通配地址。
- S3 major｜鉴权｜§1.3 UDS
  - 证据：只靠套接字文件的 0600 权限；父目录权限、残留的旧套接字、symlink 替换都没处理，而 UDS 这条路不需要 token。
  - 修复：父目录设 0700 并检查属主；启动时用 lstat 确认不是 symlink 再 unlink；accept 之后用 `getpeereid` 校验调用方 uid。
- S4 major｜DoS｜§1.4
  - 证据：没规定 websockets 的 `max_size`、握手和 hello 的超时，也没校验 `sample_rate`。只有一个会话名额，一个慢客户端就能长期占住节点。
  - 修复：限制帧长；hello 5 秒超时；`sample_rate` 只接受 16000；设定会话最长时长。
- S5 minor｜信任｜§1.6、§4-7
  - 证据：客户端在发出 Bearer token 和音频之前没有核对节点身份，地址被重新分配后，token 和音频会被发到错误的节点。
  - 修复：建立会话前比对 `/v1/info` 返回的 `node_id`，不一致就拒绝；每个节点用各自独立的 token。
- S6 minor｜注入｜§1.8
  - 证据：`<ssh-host>` 没有校验，以 `-o` 开头的参数可以注入 ssh 选项。
  - 修复：用白名单 `^[A-Za-z0-9._@-]+$` 校验，并在主机名前加 `--`。
- S7 minor｜日志｜§1.3、§1.6
  - 证据：没规定 gateway、worker 和 LaunchAgent 日志不得写入转录正文，会违反「不保存字幕」。
  - 修复：明确日志只记事件和时长，不记正文。
- S8 nit｜信息泄露｜§1.4
  - 证据：`/v1/info` 直接返回原始 `hw_uuid` 和 `user_idle_s`。
  - 修复：`hw_uuid` 改为返回哈希；`user_idle_s` 粗化成布尔值 `in_use`。

verdict: needs_fix
