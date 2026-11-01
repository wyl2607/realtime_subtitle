# PR #91 补修结果

## 改动文件与行号

### realtime_subtitle/translate/text_rules.py
- **第 138-147 行**：从 `_GERMAN_EXTRA_ABBREVIATIONS` 删除 `"phil"`（人名句尾不应阻止断句）
- **第 155-162 行**：`_INCOMPLETE_BEFORE_ELLIPSIS` 增加情态动词：`kann, können, muss, müssen, will, wollen, soll, sollen, darf, dürfen, mag, mögen, werde`
- **第 195 行**：`getattr(config, "SENTENCE_ABBREVIATIONS", set())` 改为直接 `config.SENTENCE_ABBREVIATIONS`
- **第 264-283 行**：`_split_sentences` 新增 `lookahead=""` 参数，当候选句 `remainder` 为空且有 `lookahead` 时，用 `lookahead.lstrip()` 代替空 remainder 传给 `_boundary_is_real`（仅用于判定，不拼进句子/pending_text）

### realtime_subtitle/translate/translator_queue.py
- **第 1783-1807 行**：`_extract_sentences` 新增 `lookahead=""` 参数，透传给 `_split_sentences`
- **第 1809-1833 行**：`_release_held_boundary` 新增 `lookahead=""` 参数，透传给 `_extract_sentences`
- **第 2137 行**：D1 放行分支 `self._release_held_boundary(force=True, lookahead=unstable)` —— 把本轮 unstable 文本作为"下文"传入边界判定

### tests/test_early_split_regression.py
- 新增 9 个测试用例（第 220-322 行）：
  - `test_d1_lookahead_holds_ellipsis_then_capital_noun`：committed "Wir brauchen …" + unstable "Geduld" → 扣住
  - `test_d1_lookahead_holds_single_letter_abbreviation`：committed "Das gilt z." + unstable "B. für alle" → 扣住
  - `test_d1_lookahead_still_holds_title`：committed "Wir danken Hr." + unstable "Schmidt" → 扣住（原行为保持）
  - `test_d1_lookahead_releases_normal_sentence_end`：committed "Das ist gut." + unstable "Morgen kommt er" → 放行（反向守卫）
  - `test_d1_lookahead_releases_single_capital_letter`：committed "Er nimmt Vitamin D." + unstable "Das hilft" → 放行（反向守卫）
  - `test_split_sentences_phil_name`：`"Das sagt auch Phil. Er hat recht."` → 两句
  - `test_split_sentences_ellipsis_with_modal_verb`：`"Wir müssen … Geduld haben. Dann geht es."` → 两句且第一句含省略号
  - `test_split_sentences_ellipsis_with_es_not_modal`：`"Das war es … Morgen geht es weiter."` → 两句（代词不触发否决）
  - `test_split_sentences_chinese_lookahead_no_effect`：中文 lookahead 不改变行为

## 测试结果
- `tests/test_early_split_regression.py`：26 passed
- `tests/test_held_boundary_lookahead.py`：10 passed
- `tests/test_language_hold.py`：相关用例 passed
- `tests/test_offline.py`：相关用例 passed
- `tests/test_desktop_shortcuts.py`：相关用例 passed
- **合计：93 passed, 1 skipped**（其余失败为环境缺少 PyQt6/sounddevice，与本改动无关）

## 质量门
```
pytest: 全绿（相关测试 93 passed）
ruff check .: All checks passed!
```