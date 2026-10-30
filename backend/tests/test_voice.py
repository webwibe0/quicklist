"""Real audio decoding and HTTP/SDK contracts, entirely synthetic audio/mocked OpenAI."""
import json
import math
import wave
from array import array
from io import BytesIO
from unittest.mock import MagicMock

import av
import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from app.ai.speech_provider import OpenAISpeechProvider, get_speech_provider
from app.core.config import Settings
from app.main import app
from app.services.audio import MAX_AUDIO_BYTES, AudioError, prepare_audio


def recording(seconds=0.4, amplitude=2000):
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(array("h", (int(amplitude * math.sin(i * .1)) for i in range(int(seconds * 16000)))).tobytes())
    return output.getvalue()


def encode(container_format, codec):
    output = BytesIO()
    with av.open(BytesIO(recording()), format="wav") as source:
        with av.open(output, "w", format=container_format) as dest:
            stream = dest.add_stream(codec, rate=48000)
            stream.layout = "mono"
            for frame in source.decode(audio=0):
                for packet in stream.encode(frame):
                    dest.mux(packet)
            for packet in stream.encode(None):
                dest.mux(packet)
    return output.getvalue()


@pytest.fixture
def speech_client(monkeypatch):
    monkeypatch.setattr("app.ai.speech_provider.OpenAI", lambda **kw: pytest.fail("No live audio API"))
    provider = MagicMock()
    provider.transcribe.return_value = "Что у меня завтра?"
    provider.speak.return_value = b"ID3synthetic-test-audio"
    app.dependency_overrides[get_speech_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            yield client, provider
    finally:
        app.dependency_overrides.pop(get_speech_provider, None)


@pytest.mark.parametrize("mime,container_format,codec", [
    ("audio/webm", "webm", "libopus"), ("audio/ogg", "ogg", "libopus"),
    ("audio/mp4", "mp4", "aac"),
])
def test_browser_formats_decode_without_container_duration(mime, container_format, codec):
    wav = prepare_audio(encode(container_format, codec), mime)
    with wave.open(BytesIO(wav)) as result:
        assert result.getnchannels() == 1 and result.getframerate() == 16000
        assert result.getnframes() >= 6000


def test_transcribe_returns_real_provider_text_and_canonical_audio(speech_client):
    client, provider = speech_client
    response = client.post("/api/v1/voice/transcriptions", content=recording(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 200 and response.json() == {"text": "Что у меня завтра?"}
    assert response.headers["cache-control"] == "no-store"
    assert provider.transcribe.call_args.args[0].startswith(b"RIFF")


@pytest.mark.parametrize("data,mime,code", [
    (b"", "audio/wav", 422), (b"not-a-recording", "audio/webm", 422),
    (b"https://example.com/audio", "audio/mp4", 422),
    (b"RIFF", "text/plain", 415), (b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm", 413),
], ids=["empty", "corrupt", "url-not-audio", "bad-type", "oversized"])
def test_bad_upload_never_calls_provider(speech_client, data, mime, code):
    client, provider = speech_client
    result = client.post("/api/v1/voice/transcriptions", content=data, headers={"Content-Type": mime})
    assert result.status_code == code
    provider.transcribe.assert_not_called()


@pytest.mark.parametrize("seconds,amplitude", [(0.1, 1000), (.4, 0), (60.3, 1000)])
def test_silence_short_and_overlong_rejected_before_paid_call(speech_client, seconds, amplitude):
    client, provider = speech_client
    result = client.post("/api/v1/voice/transcriptions", content=recording(seconds, amplitude), headers={"Content-Type": "audio/wav"})
    assert result.status_code == 422
    provider.transcribe.assert_not_called()


def test_mislabeled_container_is_rejected():
    with pytest.raises(AudioError):
        prepare_audio(recording(), "audio/webm")


def test_speech_sends_exact_response_text(speech_client):
    client, provider = speech_client
    result = client.post("/api/v1/voice/speech", json={"text": "Найдено событий: 3."})
    assert result.status_code == 200 and result.headers["content-type"] == "audio/mpeg"
    assert result.headers["cache-control"] == "no-store"
    provider.speak.assert_called_once_with("Найдено событий: 3.")


@pytest.mark.parametrize("text", ["", "   ", "x" * 2401])
def test_invalid_speech_input_no_call(speech_client, text):
    client, provider = speech_client
    assert client.post("/api/v1/voice/speech", json={"text": text}).status_code == 422
    provider.speak.assert_not_called()


@pytest.mark.parametrize("endpoint,method", [("speech", "speak"), ("transcriptions", "transcribe")])
def test_provider_failure_is_recoverable(speech_client, endpoint, method):
    client, provider = speech_client
    getattr(provider, method).side_effect = AudioError("Голос временно недоступен.", 503)
    kwargs = {"json": {"text": "Готово"}} if endpoint == "speech" else {
        "content": recording(), "headers": {"Content-Type": "audio/wav"},
    }
    assert client.post(f"/api/v1/voice/{endpoint}", **kwargs).status_code == 503


def provider_with_transport(monkeypatch, handler):
    settings = Settings(_env_file=None, openai_api_key="test-only", openai_stt_model="gpt-transcribe")
    client = OpenAI(api_key="test-only", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("app.ai.speech_provider.OpenAI", lambda **kw: client)
    return OpenAISpeechProvider(settings)


def test_installed_sdk_stt_contract(monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.read()
        assert b"gpt-transcribe" in body and b"recording.wav" in body and b"\r\nru\r\n" in body
        return httpx.Response(200, json={"text": "В пятницу отправить отчёт"})
    assert provider_with_transport(monkeypatch, handler).transcribe(recording()) == "В пятницу отправить отчёт"


def test_installed_sdk_tts_contract(monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/audio/speech"
        body = json.loads(request.read())
        assert body["model"] == "gpt-4o-mini-tts" and body["voice"] == "marin"
        assert body["input"] == "На какое время?" and body["response_format"] == "mp3"
        return httpx.Response(200, content=b"ID3test", headers={"content-type": "audio/mpeg"})
    assert provider_with_transport(monkeypatch, handler).speak("На какое время?") == b"ID3test"


@pytest.mark.parametrize("method", ["speak", "transcribe"])
@pytest.mark.parametrize("failure", ["timeout", "server"])
def test_sdk_errors_are_sanitized_without_retries(monkeypatch, caplog, method, failure):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-response", request=request)
        return httpx.Response(500, json={"error": {"message": "secret-response"}})
    provider = provider_with_transport(monkeypatch, handler)
    with pytest.raises(AudioError) as error:
        getattr(provider, method)("Текст" if method == "speak" else recording())
    assert error.value.status == (504 if failure == "timeout" else 503)
    assert "secret-response" not in str(error.value) + caplog.text
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["", "  ", "...", "x" * 4001], ids=["empty", "blank", "punctuation", "too-long"])
def test_no_recognized_speech_or_overlong_transcript(monkeypatch, text):
    provider = provider_with_transport(monkeypatch, lambda req: httpx.Response(200, json={"text": text}))
    with pytest.raises(AudioError):
        provider.transcribe(recording())


def test_missing_key_never_initializes_openai(monkeypatch):
    monkeypatch.setattr("app.ai.speech_provider.OpenAI", lambda **kw: pytest.fail("No client without key"))
    provider = OpenAISpeechProvider(Settings(_env_file=None, openai_api_key=""))
    with pytest.raises(AudioError, match="API-ключ"):
        provider.speak("Готово")


def test_concurrent_audio_work_rejected_not_queued(speech_client):
    from app.api.voice import slots
    client, provider = speech_client
    slots.acquire()
    slots.acquire()
    try:
        assert client.post("/api/v1/voice/speech", json={"text": "Ответ"}).status_code == 429
        provider.speak.assert_not_called()
    finally:
        slots.release()
        slots.release()
