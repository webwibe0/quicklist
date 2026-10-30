"""Speech is a transport adapter; it has no conversation or database access."""
from typing import Protocol

from openai import APITimeoutError, OpenAI, OpenAIError

from app.ai.paid_limits import budget_message, client_options, is_budget_error
from app.core.config import get_settings
from app.core.spend_budget import BudgetBlocked
from app.services.audio import AudioError


class SpeechProvider(Protocol):
    def transcribe(self, audio: bytes) -> str: ...
    def speak(self, text: str) -> bytes: ...


class OpenAISpeechProvider:
    def __init__(self, settings):
        self.settings = settings

    def client(self):
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise AudioError("Для голоса нужен API-ключ на сервере. Текстовый ввод остаётся доступен.", 503)
        return OpenAI(api_key=key, timeout=self.settings.voice_timeout_seconds, max_retries=0,
                      **client_options(self.settings))

    def transcribe(self, audio):
        try:
            with self.client() as client:
                result = client.audio.transcriptions.create(
                    model=self.settings.openai_stt_model,
                    file=("recording.wav", audio, "audio/wav"),
                    language="ru", response_format="json",
                )
            text = result.text.strip()
        except APITimeoutError:
            raise AudioError("Распознавание заняло слишком много времени. Попробуйте ещё раз.", 504) from None
        except (OpenAIError, AttributeError, ValueError, BudgetBlocked) as error:
            if is_budget_error(error):
                raise AudioError(budget_message(error), 503) from None
            raise AudioError("Не удалось распознать речь. Попробуйте ещё раз или напишите текстом.", 503) from None
        if not text or not any(char.isalnum() for char in text):
            raise AudioError("Не удалось разобрать речь. Повторите команду или напишите текстом.")
        if len(text) > 4000:
            raise AudioError("Команда слишком длинная. Запишите более короткую команду.")
        return text

    def speak(self, text):
        try:
            with self.client() as client:
                with client.audio.speech.with_streaming_response.create(
                    model=self.settings.openai_tts_model,
                    voice=self.settings.openai_tts_voice,
                    input=text, response_format="mp3",
                    instructions="Прочитай данный текст по-русски спокойно и разборчиво. Не добавляй слов.",
                ) as response:
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 5 * 1024 * 1024:
                            raise AudioError("Голосовой ответ слишком большой.", 503)
            if not chunks:
                raise AudioError("Получен пустой голосовой ответ. Нажмите воспроизведение ещё раз.", 503)
            return bytes(chunks)
        except APITimeoutError:
            raise AudioError("Озвучивание заняло слишком много времени. Текст ответа доступен.", 504) from None
        except (OpenAIError, BudgetBlocked) as error:
            if is_budget_error(error):
                raise AudioError(budget_message(error, tts=True), 503) from None
            raise AudioError(
                "Озвучивание сейчас недоступно. Текст ответа доступен.",
                503,
            ) from None


def get_speech_provider() -> SpeechProvider:
    return OpenAISpeechProvider(get_settings())
