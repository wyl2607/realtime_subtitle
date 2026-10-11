# CR-007 安全评审（lens=security）

## S1
- id: S1
- severity: major
- location: scripts/node/install_node.sh:726
- evidence: `push_windows_client` 在 714–718 行校验了 id、node_id、url_host、token、client，没有校验 port，却把 `${port}` 拼进 ssh 远端命令。`install_node_windows.sh:20` 把未过滤的 `RS_NODE_PORT` 写入 `NODE_PORT`，749 行传入。Windows OpenSSH 用 cmd.exe 执行该串；`node_setup.ps1:10` 的 `[int]$Port` 只在 powershell 起来之后才生效。port 若含 `& | < > ^ % "` 或换行，会在 powershell 之前执行。其余拼入该串的变量有白名单。token 只走 stdin，不在 argv、dry-run（文案为 `<redacted>`）或这条命令里。
- suggested_fix: 拼 `remote` 前要求 port 匹配 `^[0-9]{1,5}$` 且在 1–65535，否则 die；`RS_NODE_PORT` 同样校验。

## S2
- id: S2
- severity: major
- location: scripts/node/windows/node_setup.ps1:210
- evidence: 同 id 换成只含 id、node_id、url、token_file 的新对象（195–200 行）。其它 id 执行 `$out.Add($e)`，旧对象整份保留，含任意 `token_file` 和多余字段。单元素数组已用方括号包住（215–216 行）。token 临时文件在移动前调用了 Lock-Down。客户端 `app.py:562` 的 `read_token_file(cfg.token_file)` 不限制目录（Windows 上也不查权限），NodeClient 把读到的内容当 Bearer 发往该条目 url；url 只要求解析进 100.64.0.0/10。默认 APPDATA 其他用户写不了，但文件里已有的毒条目会被这次下发原样封存。
- suggested_fix: 写回前每条只投影白名单字段；`token_file` 必须在 `%APPDATA%\realtime_subtitle\tokens\` 下，否则丢弃该条。

已核对无另问题：不读 `GROQ_API_KEY`；key 不进日志、异常文本、状态、面板回显或 `config_local.py`。非 Windows 在 `settings_window.py:97` 的 `os.open(..., 0o600)` 写入前即限权，清除会 `unlink`（142–144 行）。两开关都关时 `open_remote_bridge`（`app.py:686`）在 import 客户端前返回。节点经 prepare 与 NodeClient 钉 IPv4，且 `trust_env=False` / `proxy=None`；云端经 `CloudClient._BearerAuth` 且 `allow_redirects=False`。icacls 为 `shell=False` 的参数数组（`settings_window.py:125`）。

verdict: needs_fix
