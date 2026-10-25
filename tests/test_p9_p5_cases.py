import json
from pathlib import Path

from realtime_subtitle.remote.routing_rules import replay_p5_case


FIXTURE = Path(__file__).with_name("fixtures") / "p5_cases.json"


def test_p5_fixture_version():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1


def test_p5_cases_match_shared_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["p5_cases"], "p5_cases.json must contain RFC P5 cases"
    for case in data["p5_cases"]:
        assert replay_p5_case(case) == case["expect"], case["name"]
