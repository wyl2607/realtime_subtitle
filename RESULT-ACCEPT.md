# v3.2.0 Mac 自动验收说明

`.governance/sprints-002-cloud-asr-first/acceptance/run_mac_auto.sh` 是执行入口：

```bash
bash .governance/sprints-002-cloud-asr-first/acceptance/run_mac_auto.sh \
  [--out DIR] [--only gates|docs|fallback] [--skip-pytest]
```

默认产物目录为 `$HOME/projects/rs-mac-native-data/accept-v320/<YYYYmmdd-HHMMSS>`，避免把测试日志和假配置误放进仓库。脚本在每个 rslite 调用前以 `HOME=$OUT/fakehome` 覆盖 HOME；真实 HOME 只读地作为节点资料来源。pytest 则保持真实 HOME，以免测试环境被沙箱改变。

## 检查项与 Done 对应

| 脚本项 | Done | 自动化内容 |
| --- | --- | --- |
| `gates_pytest`、`gates_ruff`、`gates_swift_build`、`gates_selftest`、`gates_tokens` | Done 7 | 分项记录 pytest、ruff、release build、`SELFTEST OK` 与 CI 同款设计令牌检查。 |
| `docs` | Done 8 / 15 | 版本为 3.2.0；中、英、德 README 分别验证 Groq、`--cloud on|off`、各自语言的隐私/保留用语以及 `--set-groq-key`。 |
| `fallback_bad-key` | Done 4 可自动化子集 | 假 key + 云端开启后，验证无效 key 状态、节点精修、key 不泄露与最终文本完整性。 |
| `fallback_cloud-off` | Done 4 可自动化子集 | 关闭云端后，验证不会进入 Groq 状态、仍有节点结果或节点路由选择，且最终文本完整。 |
| `fallback_node-down` | Done 4 可自动化子集 | 空节点/令牌 fakehome，验证无可用节点时落到本机、仍有 final、且不进入混合状态。 |

## 判据来源

| 判据 | 源码或文档依据 | 脚本检查 |
| --- | --- | --- |
| `--cloud on|off`、`--set-groq-key` 是配置子命令且立刻退出 | `macos-native/Sources/rslite/main.swift:54-62`、`:117-135` | 配置命令和 `--mode hybrid --source … --headless` 回放分开运行。 |
| 所有模式变化事件格式 | `macos-native/Sources/rslite/main.swift:226-231` | Python 逐行解析 `events.jsonl` 的 JSON，读取 `ev=status` 的 `text`。 |
| 本机、混合、精修、Groq、无效 key 标签 | `macos-native/Sources/rslite/HybridEngine.swift:31-38` | bad-key 要求 `模式：云端 key 无效`；cloud-off 禁止 `模式：云端·Groq`；node-down 禁止 `模式：混合·…`。 |
| 节点精修的 replace 与 final 格式 | `macos-native/Sources/rslite/main.swift:233-240` | Python 要求节点 final 的 `ev=final` 且 `text` 以 `[` 开头；bad-key 必须有该结果。 |
| 选节点 / 无可用节点路由日志 | `macos-native/Sources/rslite/NodeRouter.swift:612-615` | cloud-off 接受节点 final 或 `rslite.route select node=`；node-down 必须有 `rslite.route select local reason=no_available_node`。 |
| 所有场景无重复、无丢句及最终 WER | `scripts/bench/hybrid_score.py:220-249, 375-383` | 用 `$REPO/venv/bin/python … --json` 生成 `score.json`，Python 要求 JSON 顶层 `problems == []`，并把 `final_wer` 写进 PASS 原因。 |
| key 文件所在 fake HOME 配置目录 | `macos-native/Sources/rslite/CloudConfig.swift:22-39, 61-71` | bad-key 只经 stdin 写到 fakehome；每场景后重建 fakehome，trap 会删 `groq.key`。 |
| selftest 与设计令牌 CI 判据 | `.github/workflows/ci.yml:158-170` | selftest 输出必须含 `SELFTEST OK`；保留 `python3 scripts/design/gen_tokens.py --check`。 |

## 不能自动化的部分

- Done 4 的断网等人工故障注入，以及需要真实云端额度/网络条件的完整场景。
- Done 6 的一小时额度长测。
- Done 9–14 的 Windows 节点、跨机器交互和人工可见 UI 状态。
- Done 2–3 的性能与准确率基准，需受控设备和素材环境。

本轮仅重写自动化脚本与其验收说明；按约束未运行 rslite、pytest 或 Swift build。
