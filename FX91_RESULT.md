# FX91 补修结果：实时路径 D1 放行看不见下文

## 改了什么

### `realtime_subtitle/translate/text_rules.py`
- L146：`_GERMAN_EXTRA_ABBREVIATIONS` 删除 `"phil"`（`Phil.` 是人名句尾，应断句）。
- L160-166：`_INCOMPLETE_BEFORE_ELLIPSIS` 补情态动词
  `kann können muss müssen will wollen soll sollen darf dürfen mag mögen werde`；
  明确不加人称代词（`Das war es … Morgen …` 仍断两句）。
- L200：`getattr(config, "SENTENCE_ABBREVIATIONS", set())` → `config.SENTENCE_ABBREVIATIONS`。
- L269-303：`_split_sentences(text, final=False, lang=None, lookahead="")`。
  仅当候选句 `remainder` 为空时用 `lookahead.lstrip()` 代替去调
  `_boundary_is_real`；lookahead 只参与判定，不进入句子或 rest。
  无空格语言（zh/ja）在 `_boundary_is_real` 里恒真，行为与改前相同。

### `realtime_subtitle/translate/translator_queue.py`
- L1783：`_extract_sentences(self, final=False, lookahead="")`，透传给 `_split_sentences`。
- L1814：`_release_held_boundary(self, force=False, lookahead="")`，透传给 `_extract_sentences`。
- L2144：D1 分支改为 `_release_held_boundary(force=True, lookahead=unstable)`。
  D2 / flush / 其他调用点不传 lookahead，行为不变。

### `tests/test_early_split_regression.py`
- L224-300 新增 8 个用例：
  - D1 省略号扣留、D1 单字母缩写 `z. B.` 扣留；
  - 反向守卫：`Das ist gut.` / `Er nimmt Vitamin D.` 照常放行；
  - `Phil.` 断两句；情态动词 + 省略号断两句；代词 + 省略号仍断两句；
  - 中文 lookahead 与不传完全相同。

## 测试数

- 新增：8 项。
- 全量：`1510 passed, 33 skipped`（另有 3 个独立 GUI 脚本套件不在 pytest 收集内）。
- `ruff check .`：All checks passed。

## 质量门尾部输出

```
$ TMPDIR=/tmp ~/projects/rs-mac-venv/bin/python -m pytest -q
1510 passed, 33 skipped in 171.63s (0:02:51)

$ ~/projects/rs-mac-venv/bin/python -m ruff check .
All checks passed!
```

注：pytest 按任务给定的 `TMPDIR=/tmp` 运行；若改用工作目录下的 `.tmp/`，
`test_node_gateway` 的 AF_UNIX socket 会因路径过长（>104 字符）全部报
`OSError: AF_UNIX path too long`，与本次改动无关。
