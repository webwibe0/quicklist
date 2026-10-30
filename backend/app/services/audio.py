"""Bounded in-memory decoding. Neither container duration nor client timers are trusted."""
import wave
from array import array
from io import BytesIO
from time import monotonic

import av

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 60
SAMPLE_RATE = 16000
MIME_FORMATS = {
    "audio/webm": {"matroska", "webm"},
    "audio/ogg": {"ogg"},
    "audio/mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
    "audio/wav": {"wav"},
    "audio/x-wav": {"wav"},
}


class AudioError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def prepare_audio(data: bytes, mime: str) -> bytes:
    """Decode samples, reject video/oversize/silence, send canonical mono WAV to STT."""
    if mime not in MIME_FORMATS:
        raise AudioError("Формат записи не поддерживается. Используйте WebM, Ogg, MP4 или WAV.", 415)
    if not data:
        raise AudioError("Запись пустая. Запишите команду ещё раз.")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError("Запись слишком большая. Максимум 8 МБ.", 413)
    pcm = bytearray()
    start = monotonic()
    try:
        # Explicit demuxer prevents playlists, URLs, or probing arbitrary input formats.
        demuxer = {"audio/webm": "matroska", "audio/ogg": "ogg", "audio/mp4": "mov"}.get(mime, "wav")
        with av.open(BytesIO(data), format=demuxer, options={"protocol_whitelist": "file"}) as container:
            if not MIME_FORMATS[mime].intersection(container.format.name.split(",")):
                raise AudioError("Содержимое записи не соответствует её формату.", 415)
            if len(container.streams.audio) != 1 or container.streams.video:
                raise AudioError("Нужна запись с одной аудиодорожкой без видео.")
            stream = container.streams.audio[0]
            if stream.codec_context.channels > 2 or stream.codec_context.sample_rate > 96000:
                raise AudioError("Параметры аудио не поддерживаются.")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            source_seconds = 0.0
            first_pts = None

            def append(frame):
                pcm.extend(bytes(frame.planes[0])[:frame.samples * 2])
                if len(pcm) > MAX_SECONDS * SAMPLE_RATE * 2:
                    raise AudioError("Запись длиннее 60 секунд. Запишите более короткую команду.")

            for frame in container.decode(stream):
                source_seconds += frame.samples / frame.sample_rate
                if frame.pts is not None and frame.time_base is not None:
                    stamp = float(frame.pts * frame.time_base)
                    if first_pts is None:
                        first_pts = stamp
                    source_seconds = max(source_seconds, stamp - first_pts + frame.samples / frame.sample_rate)
                # Allow only codec padding, never a client-supplied duration override.
                if source_seconds > MAX_SECONDS + 0.1:
                    raise AudioError("Запись длиннее 60 секунд. Запишите более короткую команду.")
                if monotonic() - start > 10:
                    raise AudioError("Не удалось обработать запись вовремя. Попробуйте короче.")
                for output in resampler.resample(frame):
                    append(output)
            for output in resampler.resample(None):
                append(output)
    except AudioError:
        raise
    except (av.FFmpegError, ValueError, OverflowError, EOFError) as from_error:
        raise AudioError("Запись повреждена или имеет неподдерживаемый формат.") from from_error
    samples = array("h", pcm)
    if len(samples) < SAMPLE_RATE // 5 or not samples or max(abs(x) for x in samples) < 32:
        raise AudioError("Речь не слышна. Поднесите микрофон ближе и повторите.")
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()
