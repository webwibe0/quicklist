"""Explicit browser QA server with a scripted fake, never a production NLP fallback.

Run from backend: python -m uvicorn tests.demo_server:app --host 127.0.0.1 --port 8001
Only the exact messages listed in evals and the DoD dialogue are recognised.
"""
import json
from pathlib import Path

from app.ai.openai_provider import get_provider
from app.main import app
from tests.fakes import FakeLLMProvider

cases = json.loads((Path(__file__).resolve().parents[2] / "evals/command_cases.json").read_text(encoding="utf-8"))
commands = {case["message"]: case["parsed_fixture"] for case in cases}
commands.update({
    "Поставь завтра встречу с Иваном": {
        "intent": "CREATE_EVENT", "event_type": "MEETING",
        "title": "Встреча с Иваном", "date_expression": "завтра",
    },
    "В 15:00": {"intent": "CREATE_EVENT", "time_expression": "В 15:00"},
    "Что у меня завтра?": {"intent": "LIST_EVENTS", "period_expression": "завтра"},
})
provider = FakeLLMProvider(commands)
app.dependency_overrides[get_provider] = lambda: provider
