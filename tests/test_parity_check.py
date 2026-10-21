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
    """造一个最小仓库：两份夹具 + design/tokens.json + 生成好的 iOS 令牌。"""
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
    return tmp_path


def _make_android(tmp_path: Path, repo: Path) -> Path:
    android = tmp_path / "android"
    (android / "design").mkdir(parents=True)
    (android / "design" / "tokens.json").write_bytes((repo / "design" / "tokens.json").read_bytes())
    target = android / "core" / "src" / "test" / "resources" / "fixtures"
    target.mkdir(parents=True)
    for source in (repo / "tests" / "fixtures").glob("*.json"):
        (target / source.name).write_bytes(source.read_bytes())
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
    android = _make_android(tmp_path, repo)

    assert parity.main(["--android", str(android)], root=repo) == 0
    assert "PARITY OK" in capsys.readouterr().out


def test_android_drift_fails(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo)
    (android / "core" / "src" / "test" / "resources" / "fixtures" / "p5_cases.json").write_text(
        '{"cases": [3]}\n', encoding="utf-8"
    )

    assert parity.main(["--android", str(android)], root=repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_android_missing_tokens(parity, generator, tmp_path, capsys):
    repo = _make_repo(tmp_path, generator)
    android = _make_android(tmp_path, repo)
    (android / "design" / "tokens.json").unlink()

    assert parity.main(["--android", str(android)], root=repo) == 1
    assert "MISSING" in capsys.readouterr().out
