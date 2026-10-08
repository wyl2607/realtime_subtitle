# TK-002 — 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
实现常驻的轻量网关（<50MB，不 import numpy/mlx）。它监听 Tailscale IP:8791 和本机 UDS，对外提供 /v1/info 和 /v2/session；有会话时 spawn worker，会话结束空闲 120s 后回收 worker。

## Context
- 先读 RFC 的 P1、P2、P3 和「安全决策」S2/S3/S4/S8。
- 和 worker 之间只走 P3 管道协议；测试里用**假 worker**（一个 python 脚本）代替。
- 复用的代码：v1 `realtime_subtitle/remote/server.py` 里的 `validate_host`、hmac 比较和 1013 busy 逻辑，可以参考或搬过来；v1 文件本身由 TK-006 删除。
- info.py 负责收集能力和状态：hw_hash、in_use（基于 HIDIdleTime）、on_ac（基于 IOPS 或 pmset）、node_id（从安装时生成的文件读取）、rtf（来自状态文件）。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - realtime_subtitle/node/gateway.py, info.py, __init__.py, requirements.txt
  - tests/test_node_gateway.py
- 禁止修改：
  - realtime_subtitle/node/worker.py, segmenter.py, engines.py（归 TK-001）
  - realtime_subtitle/remote/**（归 TK-006）
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] 生命周期：没有会话时没有 worker 进程；有会话就 spawn；会话结束 120s 后先 SIGTERM，3s 后 SIGKILL（测试时把时长调短）；ping 超时（10s+10s）判定会话断开。
- [ ] 鉴权：token 错误返回 401；第二个会话收到 1013；建立 UDS 时检查目录 0700 和属主、用 lstat 拒绝 symlink、accept 后用 getpeereid 校验 uid；UDS 不需要 token。
- [ ] 上限：max_size 64KB；hello 5s 超时；sample_rate/format 不对就拒绝；会话最长 4h（测试调短）；队列超限时丢最旧的帧并告警。
- [ ] 绑定：启动时用 `tailscale ip -4` 取地址再过 validate_host；bind 失败时指数退避，最长 60s；任何情况下都不绑 0.0.0.0 或 ::。
- [ ] /v1/info 只返回 hw_hash 和 in_use（S8）；日志里没有正文（S7）。
- [ ] 常驻 RSS < 50MB（用真机 ps 实测）。
- [ ] 全量 pytest 和 ruff 通过。

## 验证命令
```bash
QT_QPA_PLATFORM=offscreen ~/projects/rs-mac-venv/bin/python -m pytest -q
~/projects/rs-mac-venv/bin/ruff check .
```

## 状态历史
- 2026-10-08 CST: planned
