import json
from pathlib import Path

from realtime_subtitle.remote.routing_rules import replay_route_case


FIXTURE = Path(__file__).with_name("fixtures") / "route_cases.json"


def test_route_fixture_version():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1


def test_route_cases_match_shared_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["route_cases"], "route_cases.json must contain RFC ARCH-01 cases"
    for case in data["route_cases"]:
        assert replay_route_case(case) == [step["expect"] for step in case["steps"]], case["name"]
