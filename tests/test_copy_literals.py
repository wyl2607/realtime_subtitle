import json

from scripts.design import check_copy_literals


def test_copy_literal_check_passes_for_repository():
    assert check_copy_literals.main([]) == 0


def test_copy_literal_check_detects_a_hard_coded_route_status(tmp_path, capsys):
    (tmp_path / "design").mkdir()
    (tmp_path / "realtime_subtitle").mkdir()
    (tmp_path / "design" / "tokens.json").write_text(
        json.dumps({"text": {"statusCloud": "云端·Groq"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "realtime_subtitle" / "fake.py").write_text(
        'status = "云端·Groq"\n', encoding="utf-8"
    )

    assert check_copy_literals.main(["--root", str(tmp_path)]) == 1
    assert "realtime_subtitle/fake.py:1  statusCloud" in capsys.readouterr().out



def _android_fixture(tmp_path, files):
    """最小仓库 + Android 仓库；files 是 {相对 Android 根的路径: 内容}。"""
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "tokens.json").write_text(
        json.dumps({"text": {"statusLocal": "本机"}}, ensure_ascii=False), encoding="utf-8"
    )
    android = tmp_path / "android"
    for relative, content in files.items():
        path = android / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    android.mkdir(exist_ok=True)
    return ["--root", str(tmp_path), "--android", str(android)]


def test_android_scans_every_module_with_portable_paths(tmp_path, capsys):
    args = _android_fixture(tmp_path, {
        "core/src/main/kotlin/a/Core.kt": 'val label = "本机"\n',
        "engine/src/main/kotlin/a/Engine.kt": 'val label = "本机"\n',
    })

    assert check_copy_literals.main(args) == 1
    out = capsys.readouterr().out
    assert "android:core/src/main/kotlin/a/Core.kt:1  statusLocal" in out
    assert "android:engine/src/main/kotlin/a/Engine.kt:1  statusLocal" in out


def test_android_skips_generated_build_and_nested_worktrees(tmp_path, capsys):
    args = _android_fixture(tmp_path, {
        "core/src/main/kotlin/com/wyl26/dezhsubtitle/core/DesignTokens.kt": 'const val statusLocal = "本机"\n',
        "app/src/main/build/Gen.kt": 'val label = "本机"\n',
        ".claude/worktrees/x/app/src/main/kotlin/a/Copy.kt": 'val label = "本机"\n',
    })

    assert check_copy_literals.main(args) == 0
    assert "COPY LITERALS OK" in capsys.readouterr().out


def test_android_allow_uses_prefixed_relative_key(tmp_path, monkeypatch):
    args = _android_fixture(tmp_path, {"app/src/main/kotlin/a/Ui.kt": 'val label = "本机"\n'})
    monkeypatch.setitem(
        check_copy_literals.ALLOW, "android:app/src/main/kotlin/a/Ui.kt:statusLocal", "测试豁免"
    )

    assert check_copy_literals.main(args) == 0


def test_android_missing_path_is_an_error(tmp_path, capsys):
    args = _android_fixture(tmp_path, {})
    args[-1] = str(tmp_path / "nonexistent")

    assert check_copy_literals.main(args) == 2
    assert "Android 路径不存在" in capsys.readouterr().err
