from app.schemas.commands import Intent, ParsedCommand


class FakeLLMProvider:
    """Scripted test double. No NLP claims and no production fallback."""
    is_test_double = True

    def __init__(self, commands: dict[str, dict]):
        self.commands = commands
        self.contexts = []

    def parse_command(self, text: str, context: dict) -> ParsedCommand:
        self.contexts.append(context)
        return ParsedCommand.model_validate(self.commands.get(text, {"intent": Intent.UNKNOWN}))
