import json
from pathlib import Path

from scripts.run_evals import assess

from app.schemas.commands import ParsedCommand


def test_all_offline_fixtures_resolve_with_expected_business_outcome():
    cases = json.loads((Path(__file__).resolve().parents[2] / "evals/command_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 20
    failures = [report for case in cases
                if not (report := assess(case, ParsedCommand.model_validate(case["parsed_fixture"]))) ["passed"]]
    assert not failures, failures
