"""跨端一致性检查的行为（spec 010 C3/C4）：一致 / 漂移 / 缺失 / --allow-missing / --android。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> object:
    path = ROOT / "scripts" / "design" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_design_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def parity():
    return _load("check_parity")


@pytest.fixture(scope="module")
def generator():
    return _load("gen_tokens")


def _make_repo(tmp_path: Path, generator) -> Path:
    """造一个最小仓库：两份夹具 + design/tokens.json + 生成好的 iOS/rslite/Python 令牌。"""
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "route_cases.json").write_text('{"cases": [1]}\n', encoding="utf-8")
    (fixtures / "p5_cases.json").write_text('{"cases": [2]}\n', encoding="utf-8")

    ios_fixtures = tmp_path / "ios" / "Tests" / "fixtures"
    ios_fixtures.mkdir(parents=True)
    for name in ("route_cases.json", "p5_cases.json"):
        (ios_fixtures / name).write_bytes((fixtures / name).read_bytes())

    design = tmp_path / "design"
    design.mkdir()
    tokens = json.loads((ROOT / "design" / "tokens.json").read_text(encoding="utf-8"))
    (design / "tokens.json").write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")

    shared = tmp_path / "ios" / "Shared"
    shared.mkdir(parents=True)
    (shared / "DesignTokens.swift").write_text(generator.render_swift(tokens), encoding="utf-8")

    rslite = tmp_path / "macos-native" / "Sources" / "rslite"
    rslite.mkdir(parents=True)
    (rslite / "DesignTokens.swift").write_text(generator.render_swift(tokens), encoding="utf-8")

    py_ui = tmp_path / "realtime_subtitle" / "ui"
    py_ui.mkdir(parents=True)
    (py_ui / "design_tokens.py").write_text(generator.render_python(tokens), encoding="utf-8")
    return tmp_path


def _make_android(tmp_path: Path, repo: Path, generator) -> Path:
    android = tmp_path / "android"
    (android / "design").mkdir(parents=True)
    (android / "design" / "tokens.json").write_bytes((repo / "design" / "tokens.json").read_bytes())
    target = android / "core" / "src" / "test" / "resources" / "fixtures"
    target.mkdir(parents=True)
    for source in (repo / "tests" / "fixtures").glob("*.json"):
        (target / source.name).write_bytes(source.read_bytes())

    tokens = json.loads((repo / "design" / "tokens.json").read_text(encoding="utf-8"))
    kotlin = android / generator.ANDROID_KT_REL
    kotlin.parent.mkdir(parents=True)
    kotlin.write_text(generator.render_kotlin(tokens), encoding="utf-8")
    return android


def test_repo_parity_ok(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    assert parity.main([], root=repo) == 0
    assert "PARITY OK" in capsys.readouterr().out


def test_repo_drift_reports_first_line(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    target = repo / "ios" / "Tests" / "fixtures" / "route_cases.json"
    target.write_text(target.read_text(encoding="utf-8") + "extra\n", encoding="utf-8")

    assert parity.main([], root=repo) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "第 2 行" in out


def test_missing_copy_fails_by_default(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    (repo / "ios" / "Tests" / "fixtures" / "p5_cases.json").unlink()

    assert parity.main([], root=repo) == 1
    assert "MISSING" in capsys.readouterr().out


def test_allow_missing_only_warns(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    (repo / "ios" / "Tests" / "fixtures" / "p5_cases.json").unlink()

    assert parity.main(["--allow-missing"], root=repo) == 0
    out = capsys.readouterr().out
    assert "MISSING" in out and "WARN" in out
    assert "PARITY OK" in out


def test_android_parity_ok(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo, generator)

    assert parity.main(["--android", str(android)], root=repo) == 0
    assert "PARITY OK" in capsys.readouterr().out


def test_android_drift_fails(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo, generator)
    (android / "core" / "src" / "test" / "resources" / "fixtures" / "p5_cases.json").write_text(
        '{"cases": [3]}\n', encoding="utf-8"
    )

    assert parity.main(["--android", str(android)], root=repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_android_missing_tokens(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo, generator)
    (android / "design" / "tokens.json").unlink()

    assert parity.main(["--android", str(android)], root=repo) == 1
    assert "MISSING" in capsys.readouterr().out


def test_rslite_swift_drift_reports_first_line(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    target = repo / "macos-native" / "Sources" / "rslite" / "DesignTokens.swift"
    target.write_text(target.read_text(encoding="utf-8") + "// extra\n", encoding="utf-8")

    assert parity.main([], root=repo) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "rslite/DesignTokens.swift" in out


def test_python_tokens_drift_fails(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    target = repo / "realtime_subtitle" / "ui" / "design_tokens.py"
    target.write_text(target.read_text(encoding="utf-8") + "EXTRA = 1\n", encoding="utf-8")

    assert parity.main([], root=repo) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "design_tokens.py" in out


def test_rslite_missing_allowed(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    (repo / "macos-native" / "Sources" / "rslite" / "DesignTokens.swift").unlink()

    assert parity.main(["--allow-missing"], root=repo) == 0
    out = capsys.readouterr().out
    assert "WARN MISSING" in out
    assert "PARITY OK" in out


def test_android_kotlin_drift_fails(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo, generator)
    kotlin = android / generator.ANDROID_KT_REL
    kotlin.write_text(kotlin.read_text(encoding="utf-8").replace(
        "object Color {", "object Color { // drift"
    ), encoding="utf-8")

    assert parity.main(["--android", str(android)], root=repo) == 1
    out = capsys.readouterr().out
    assert "DRIFT" in out
    assert "DesignTokens.kt" in out


def test_android_tokens_and_kotlin_self_consistent_but_drifted(parity, generator, tmp_path, capsys):
    """Android 自己改了 tokens 并重新生成 Kotlin（自洽），仍应两处都报 DRIFT。"""
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo, generator)
    tokens_path = android / "design" / "tokens.json"
    tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
    tokens["color"]["subtitleSource"] = "#123456"
    tokens_path.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    (android / generator.ANDROID_KT_REL).write_text(generator.render_kotlin(tokens), encoding="utf-8")

    assert parity.main(["--android", str(android)], root=repo) == 1
    out = capsys.readouterr().out
    assert out.count("DRIFT") == 2
    assert "tokens.json" in out
    assert "DesignTokens.kt" in out


def test_new_repo_fixture_without_ios_copy_is_missing(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    (repo / "tests" / "fixtures" / "new_cases.json").write_text('{"cases": [9]}\n', encoding="utf-8")

    assert parity.main([], root=repo) == 1
    assert "MISSING ios/Tests/fixtures/new_cases.json" in capsys.readouterr().out


def test_crlf_checkout_is_not_drift(tmp_path):
    """Windows 签出（autocrlf）把副本变成 CRLF，内容没变，不能报 DRIFT。"""
    from pathlib import Path as _P
    src = _P(__file__).resolve().parents[1]
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_parity_crlf", src / "scripts/design/check_parity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_bytes(b'{\n  "x": 1\n}\n')
    b.write_bytes(b'{\r\n  "x": 1\r\n}\r\n')
    assert mod._compare_file(a, b, tmp_path, allow_missing=False)
    b.write_bytes(b'{\r\n  "x": 2\r\n}\r\n')
    assert not mod._compare_file(a, b, tmp_path, allow_missing=False)
