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
