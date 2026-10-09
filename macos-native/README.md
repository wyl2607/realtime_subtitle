# rsbench

```bash
cd macos-native && swift build -c release
python ../scripts/bench/native_export.py --parquet ../rs-mac-native-data/fleurs_de_80.parquet --n 60 --wav-dir /tmp/rs-wav --refs /tmp/rs-refs.jsonl
.build/release/rsbench asr --wav-dir /tmp/rs-wav --refs /tmp/rs-refs.jsonl --locale de-DE --out /tmp/native_asr.jsonl --volatile
.build/release/rsbench translate --pairs ../rs-mac-native-data/de_zh_pairs.json --n 40 --src de --dst zh-Hans --out /tmp/native_translate.jsonl
python ../scripts/bench/native_score.py --asr-native /tmp/native_asr.jsonl --asr-refs /tmp/rs-refs.jsonl --asr-baseline ../rs-mac-native-data/asr_results.jsonl --translate-native /tmp/native_translate.jsonl --translate-baseline ../rs-mac-native-data/translate_results.jsonl
```
翻译语言未安装时，先到“系统设置 -> 通用 -> 语言与地区 -> 翻译语言”下载。

rstranslate（常驻翻译服务）：stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应。
`swift build -c release --product rstranslate`，然后 `echo '{"id":1,"op":"translate","src":"de","dst":"zh-Hans","text":"Guten Morgen"}' | .build/release/rstranslate`。
op 为 `translate` 或 `status`；失败返回 `{"id":..,"error":..}` 且进程不退出；启动先输出 `{"ready":true}`。

rslite（实时字幕）：`swift build -c release --product rslite`。
真机界面：`.build/release/rslite --source tap --src de-DE --dst zh-Hans`；当前分支 `tap/mic` 会提示“采集模块未接入”。
文件压测：`.build/release/rslite --headless --source file:/path/to/audio.wav --src de-DE --dst zh-Hans --mode local`。
headless 输出 JSONL：`volatile/final/translation/status`，文件结束后等待最后一句翻译完成再退出。
混合精修：安装脚本写 `~/.config/rslite/nodes.json`（0600）后运行 `.build/release/rslite --source tap --mode auto`；也可用 `--mode local` 强制本机、`--mode hybrid` 强制尝试节点精修。
节点清单格式：`[{"id":"mini2","node_id":"...","url":"ws://mini2.tailnet:8791/v2/session","token_file":"~/.config/rslite/mini2-token"}]`。token 文件必须是 0600；客户端先查 `/v1/info` 校验 `node_id`，不一致就不建立会话、不发送音频。
菜单栏显示「字·本机」「字·混合·<节点>」「字·精修中」；路由选择理由写 stderr，日志不写转录正文或译文。
自检：`.build/release/rslite --selftest` 会跑混合替换、P8 能力门槛、节点打分和迁移门槛的纯逻辑检查。
