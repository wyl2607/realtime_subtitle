### 1. Done criteria 第 1 条「一键安装」对齐矩阵

| 子项 | [install_node.sh](file:///Users/yumei/projects/rs-tk-007/scripts/node/install_node.sh) 行号 | [test_install_node.py](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py) 用例 | 判定 | 依据与证据说明 |
|---|---|---|---|---|
| **mini2 开机自启** | L32, L424, L541–558 (`plist` 渲染安装、`launchctl bootstrap`/`kickstart`) | [`test_plist_template_contract`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L210-L224)<br>[`test_success_twice_keeps_single_nodes_json_entry_and_updates_it`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L514-L541)<br>[`test_no_prev_bootstrap_failure_removes_plist_keeps_failed_and_relaunches_v1`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L650-L681) | **partial** | **单元测试**通过桩断言了 plist 的 `KeepAlive` 契约和 `launchctl bootstrap` 调用；<br>**真机证据**：2026-10-09 20:19 mini2（34cbbfa）重装成功并通过步骤 10 验证 TCP 监听，证明当前会话被拉起；但尚未在 mini2 **整机重启**后验证 LaunchAgent 自动拉起。 |
| **两端 token 0600** | L673–685 (`write_secret_file`)<br>L775–804 (`step_token`) | [`test_token_stays_out_of_argv_and_selfcheck_failure_keeps_old_version`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L377-L412)<br>[`test_existing_token_reuse_tightens_permissions`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L770-L784)<br>[`test_malformed_existing_token_is_regenerated_on_both_sides`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L786-L800) | **covered** | **单元测试**对目标机 `~/.config/rs-node/token` 与客户端 `~/.config/rslite/tokens/mini2.token` 显式断言权限 `0o600`（目录 `0o700`），并覆盖了生成、权限过宽时收紧、格式异常重生成。<br>**真机证据**：重装时 gateway 正常启动（非 0600 会被 gateway 拒绝加载）。 |
| **写 nodes.json** | L640–670 (`merge_nodes_json`)<br>L857–870 (`step_nodes_json`) | [`test_merge_nodes_json_create_dedupe_and_perms`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L328-L351)<br>[`test_merge_nodes_json_refuses_to_clobber_non_list`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L352-L363)<br>[`test_success_twice_keeps_single_nodes_json_entry_and_updates_it`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L514-L541)<br>[`test_token_stays_out_of_argv_and_selfcheck_failure_keeps_old_version`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L410)<br>[`test_with_prev_info_failure_restores_old_dir_and_rebootstraps`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L718) | **covered** | **单元测试**完整覆盖了 `nodes.json` 的新建、原子写入、权限 `0600`、按 `id` 去重更新、非 list 拒改保护，以及安装失败/回滚时不污染现有清单。<br>**真机证据**：安装后客户端配置更新成功。 |
| **二次执行幂等** | L94–108 (`NODE_ID_SH`)<br>L526–538 (`SWAP_SH` 轮转 `.prev`)<br>L766–771, L775–804 (沿用 node_id/token)<br>L887 (保留上一版提示) | [`test_existing_target_token_is_reused`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L413-L425)<br>[`test_node_id_is_reused_on_rerun`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L426-L434)<br>[`test_success_twice_keeps_single_nodes_json_entry_and_updates_it`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L514-L541)<br>[`test_first_install_does_not_claim_prev_and_second_does`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L542-L551)<br>[`test_existing_token_reuse_tightens_permissions`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L770-L784) | **covered** | **单元测试**对连续执行两次安装进行了完整断言（node_id/token 沿用、保留上一版为 `~/rs-node.prev`、`nodes.json` 原位更新不追加）；<br>**真机证据**：mini2 于 20:19 依据 34cbbfa 完成覆盖重装，未产生冲突。 |
| **--local 可用** | L10, L37, L623–628 (`tgt_run` 本机分支)<br>L718–723 (`parse_args`)<br>L755–758 (`step_preflight`)<br>L858–861 (`step_nodes_json` 跳过写清单) | [`test_arg_errors`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L276-L283)<br>[`test_dry_run_local_skips_nodes_json`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L309-L317) | **partial** | **单元测试**仅断言了 `--local` 的参数互斥报错和 dry-run 分支（验证不写 nodes.json），**缺少** `--local` 模式在桩环境中的端到端安装测试（目前全流程用例均跑 `_run(["mini2"], ...)`）；<br>**真机证据**：暂无（尚未在本机执行过 `--local` 端到端安装）。 |
| **人为制造自检失败能回滚** | L501–523 (`rollback`)<br>L539, L556, L570, L604 (启动与校验失败触发回滚)<br>L826–830 (自检失败退出并清理 `.new`) | [`test_token_stays_out_of_argv_and_selfcheck_failure_keeps_old_version`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L377-L412)<br>[`test_tcp_not_listening_rolls_back`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L605-L621)<br>[`test_tcp_listening_on_wrong_pid_or_ip_rolls_back`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L622-L631)<br>[`test_wildcard_listener_rolls_back_even_if_correct_ip_also_listens`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L632-L641)<br>[`test_no_prev_bootstrap_failure_removes_plist_keeps_failed_and_relaunches_v1`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L650-L681)<br>[`test_with_prev_info_failure_restores_old_dir_and_rebootstraps`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L694-L729)<br>[`test_prev_rename_failure_rebootstraps_old_plist`](file:///Users/yumei/projects/rs-tk-007/tests/test_install_node.py#L730-L750) | **partial** | **单元测试**覆盖极其详尽（7 个用例分别覆盖了模型加载失败不碰旧版、TCP 未监听回滚、`/v1/info` 超时恢复 `.prev` 并重启旧 LaunchAgent、恢复旧版 v1 等）；<br>但 RFC Done criteria 明确要求**“人为制造一次自检失败，能够回滚”**作为验收项，**真机上尚未实施过故障注入演练**。 |

---

### 2. 缺口清单（需真机操作与执行方案）

1. **mini2 开机自启验收**
   - **为何必须真机**：`launchctl` 守护 LaunchAgent 依赖当前登录用户的 GUI 会话，桩测试无法模拟 macOS 系统重启及用户登录过程中的 launchd 调度。
   - **需要操作**：
     1. 重启 mini2：`ssh mini2 'sudo reboot'` 或物理重启。
     2. 用户登录 mini2 桌面后，在 MacBook Air 执行验证命令：
        ```bash
        ssh mini2 'launchctl print gui/$(id -u)/com.realtimesubtitle.node | grep "state = running"'
        ssh mini2 'lsof -nP -iTCP:8791 -sTCP:LISTEN'
        ```
     3. 确认 gateway 进程在开机/登录后自启且端口正常监听。

2. **`--local` 本机安装端到端验证**
   - **为何必须真机**：桩测试未跑过 `--local` 全流程，本机 MacBook Air 尚未验收过安装到 `~/rs-node`、不写 `nodes.json` 且 UDS `/v1/info` 能通。
   - **需要操作**：
     1. 在 MacBook Air 执行预检演练：`bash scripts/node/install_node.sh --dry-run --local`。
     2. 实机执行本地安装：`bash scripts/node/install_node.sh --local`。
     3. 验证本地状态：确认生成 `~/Library/LaunchAgents/com.realtimesubtitle.node.plist`、UDS `/v1/info` 返回 200，且 `~/.config/rslite/nodes.json` 没有写入本机记录。

3. **人为制造自检失败并验证回滚演练**
   - **为何必须真机**：Done criteria 第 1 条要求实机故障注入证明回滚链路可用（有 `.prev` 时退回 `.prev` 并拉起旧服务）。
   - **需要操作**：
     1. 保证 mini2 当前有一个正常运行的 `~/rs-node`（作为 `.prev` 候选）。
     2. 制造故障注入（例如在目标端临时破坏预置环境，或执行带有必然失败自检指令的分支，或在目标机临时修改环境变量使 TCP 监听校验超时）：
        ```bash
        # 示例：通过环境变量让自检等待超时注入失败
        RS_INFO_WAIT=2 bash scripts/node/install_node.sh mini2  # 或在目标机临时阻断端口引发 rollback
        ```
     3. 观察控制端输出 `ROLLBACK：... 已回滚到上一版并重新启动`。
     4. 在 mini2 检查：确认 `~/rs-node` 恢复为旧版、`com.realtimesubtitle.node` 恢复为运行状态、`curl --unix-socket ~/Library/Application\ Support/rs-node/gw.sock http://localhost/v1/info` 仍返回 200。

---

### 3. 命令运行与真实输出
