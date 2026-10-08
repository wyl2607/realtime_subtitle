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
