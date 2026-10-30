"""AI evals (default) or explicit offline fixture validation (--offline)."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.openai_provider import OpenAILLMProvider  # noqa: E402
from app.ai.provider import IntentParser, ProviderError, normalize_command  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.schemas.commands import ParsedCommand  # noqa: E402
from app.services.validator import CommandValidator, normalize_action  # noqa: E402
from app.temporal.resolver import TemporalResolver  # noqa: E402


def assess(case: dict, command: ParsedCommand) -> dict:
    command = normalize_command(command)
    temporal = TemporalResolver(get_settings().app_timezone).resolve(command, datetime.fromisoformat(case["now"]))
    command = normalize_action(command, temporal)
    _, missing = CommandValidator().validate(command, temporal, case["message"])
    actual = command.model_dump(mode="json") | temporal.model_dump(mode="json") | {"missing_fields": missing}
    checks = {}
    for key, expected in case["expected"].items():
        if key == "title_contains":
            checks[key] = all(value.casefold() in (command.title or "").casefold() for value in expected)
        elif key == "query_contains":
            checks[key] = all(value.casefold() in (command.search_query or "").casefold() for value in expected)
        elif key == "missing_fields":
            checks[key] = set(expected) == set(missing)
        else:
            checks[key] = actual.get(key) == expected
    return {"id": case["id"], "passed": all(checks.values()), "checks": checks, "actual": actual}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Validate fixtures/temporal/business rules; NOT model quality")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    if not args.offline and not settings.openai_api_key.get_secret_value():
        print(json.dumps({"mode": "openai", "status": "blocked", "model": settings.openai_model,
                          "reason": "OPENAI_API_KEY is absent; run --offline for fixture checks"}))
        return 2
    cases = json.loads((ROOT / "evals/command_cases.json").read_text(encoding="utf-8"))
    provider = IntentParser(OpenAILLMProvider()) if not args.offline else None
    results = []
    started = time.perf_counter()
    for case in cases:
        try:
            command = provider.parse(case["message"], case.get("context", {})) if provider else ParsedCommand.model_validate(case["parsed_fixture"])
            result = assess(case, command)
        except ProviderError as exc:
            result = {"id": case["id"], "passed": False, "error": str(exc)}
        results.append(result)
        print(f"{'PASS' if result['passed'] else 'FAIL'} {case['id']}", flush=True)
    passed = sum(result["passed"] for result in results)
    report = {
        "mode": "offline_fixture_validation" if args.offline else "openai",
        "model": None if args.offline else settings.openai_model,
        "cases": len(results), "passed": passed, "case_pass_rate": passed / len(results),
        "intent_accuracy": None if args.offline else sum(result.get("checks", {}).get("intent", False) for result in results) / len(results),
        "provider_errors": sum("error" in result for result in results),
        "elapsed_seconds": round(time.perf_counter() - started, 2), "results": results,
    }
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
