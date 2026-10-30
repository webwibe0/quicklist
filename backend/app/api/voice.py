import asyncio
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.ai.speech_provider import SpeechProvider, get_speech_provider
from app.services.audio import MAX_AUDIO_BYTES, MIME_FORMATS, AudioError, prepare_audio
from app.services.authentication import require_user

router = APIRouter(prefix="/api/v1/voice", tags=["voice"], dependencies=[Depends(require_user)])
# Local single-user prototype: reject concurrent work rather than queue paid requests.
slots = BoundedSemaphore(2)


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2400)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Текст ответа пуст.")
        return value.strip()


def voice_work(function):
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Голосовой сервис занят. Повторите через несколько секунд.")
    try:
        return function()
    except AudioError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    finally:
        slots.release()


@router.post("/transcriptions")
async def transcribe(request: Request, provider: SpeechProvider = Depends(get_speech_provider)):
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in MIME_FORMATS:
        raise HTTPException(415, "Формат записи не поддерживается.")
    # Raw binary body avoids unbounded multipart spooling and never writes recordings to disk.
    data = bytearray()
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_AUDIO_BYTES:
                    raise HTTPException(413, "Запись слишком большая. Максимум 8 МБ.")
    except TimeoutError:
        raise HTTPException(408, "Загрузка записи заняла слишком много времени.") from None
    text = await run_in_threadpool(
        voice_work, lambda: provider.transcribe(prepare_audio(bytes(data), mime)),
    )
    return Response(content=Transcription(text=text).model_dump_json(),
                    media_type="application/json", headers={"Cache-Control": "no-store"})


class Transcription(BaseModel):
    text: str


@router.post("/speech")
def speech(request: SpeechRequest, provider: SpeechProvider = Depends(get_speech_provider)):
    audio = voice_work(lambda: provider.speak(request.text))
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
