"""Shared on one VPS filesystem. Zero allowance fails closed; never auto-renews."""
import io
import json
import wave
from threading import BoundedSemaphore

import httpx

from app.core.spend_budget import BudgetBlocked, SpendBudget

BUDGET_MESSAGE = 'Лимит AI-бюджета недоступен или исчерпан. Лимит не продлевается автоматически.'
# One production web process; worker has its own separate budget/interval guard.
NETWORK_SLOTS = BoundedSemaphore(2)


def is_budget_error(error):
    return isinstance(error, BudgetBlocked) or isinstance(error.__cause__, BudgetBlocked)


def budget_message(error, *, tts=False):
    failure = error if isinstance(error, BudgetBlocked) else error.__cause__
    code = str(failure) if isinstance(failure, BudgetBlocked) else ''
    if code == 'BUDGET_BUSY':
        return 'AI-сервис занят другими запросами. Повторите через несколько секунд.'
    if code == 'BUDGET_EXHAUSTED':
        return ('Лимит AI-бюджета: остатка недостаточно для безопасного резерва озвучивания. '
                'Текст ответа доступен. Ожидание не увеличит выделенный бюджет.' if tts else
                'Лимит AI-бюджета: остатка недостаточно для этого запроса. '
                'Ожидание не увеличит выделенный бюджет.')
    if code == 'BUDGET_STORAGE_UNAVAILABLE':
        return 'Не удалось безопасно проверить или сохранить учёт AI-бюджета. Обратитесь к владельцу сервиса.'
    return BUDGET_MESSAGE


class LimitedTransport(httpx.BaseTransport):
    def __init__(self, settings, inner=None):
        self.settings = settings
        self.budget = SpendBudget(settings.interactive_budget_path,
            limit_nanos=round(settings.interactive_budget_usd * 1e9),
            max_calls=settings.interactive_budget_max_calls,
            enforce_limits=settings.ai_usage_mode == 'capped')
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        if not NETWORK_SLOTS.acquire(blocking=False):
            raise BudgetBlocked('BUDGET_BUSY')
        try:
            return self._handle_request(request)
        finally:
            NETWORK_SLOTS.release()

    def _handle_request(self, request):
        raw = request.read()
        if request.url.host != 'api.openai.com' or request.url.scheme != 'https' or len(raw) > 3_000_000:
            raise BudgetBlocked('UNAPPROVED_PAID_REQUEST')
        path = request.url.path
        data = json.loads(raw) if 'application/json' in request.headers.get('content-type', '') else None
        if path.endswith('/responses'):
            if data['model'] != 'gpt-5.6-luna' or not 512 <= data.get('max_output_tokens', 0) <= 8192 or len(raw) > 200000:
                raise BudgetBlocked('UNPRICED_NLU_REQUEST')
            kind, reserve = 'nlu', (len(raw) + 256) * 250 + data['max_output_tokens'] * 1200
        elif path.endswith('/embeddings'):
            if data['model'] != 'text-embedding-3-large' or data['dimensions'] != 1536:
                raise BudgetBlocked('UNPRICED_EMBEDDING_REQUEST')
            texts = data['input'] if isinstance(data['input'], list) else [data['input']]
            if len(texts) > 16 or any(len(t.encode()) > 8000 for t in texts):
                raise BudgetBlocked('EMBEDDING_INPUT_LIMIT')
            kind, reserve = 'embedding', sum(len(t.encode()) + 32 for t in texts) * 130
        elif path.endswith('/audio/transcriptions'):
            if b'gpt-transcribe' not in raw or b'RIFF' not in raw:
                raise BudgetBlocked('UNPRICED_AUDIO_REQUEST')
            with wave.open(io.BytesIO(raw[raw.index(b'RIFF'):]), 'rb') as audio:
                if audio.getnframes() / audio.getframerate() > 60:
                    raise BudgetBlocked('AUDIO_DURATION_LIMIT')
            kind, reserve = 'stt', 4_500_000
        elif path.endswith('/audio/speech'):
            if data['model'] != 'gpt-4o-mini-tts' or len(data['input']) > 2400:
                raise BudgetBlocked('UNPRICED_TTS_REQUEST')
            # No reliable caller output-token cap/usage on this endpoint. Retain the
            # whole per-call allowance, including on success. Not an account USD-stop.
            kind, reserve = 'tts', 1_000_000_000
        else:
            raise BudgetBlocked('UNAPPROVED_PAID_ENDPOINT')
        ticket = self.budget.reserve(kind, reserve)
        response = self.inner.handle_request(request)
        if response.status_code == 200 and kind in {'nlu', 'embedding', 'stt'}:
            payload = json.loads(response.read())
            usage = payload.get('usage', {})
            if kind in {'nlu', 'embedding'}:
                inputs = usage.get('total_tokens' if kind == 'embedding' else 'input_tokens')
                outputs = 0 if kind == 'embedding' else usage.get('output_tokens')
                if type(inputs) is int and type(outputs) is int and min(inputs, outputs) >= 0:
                    self.budget.finish(ticket, inputs * (130 if kind == 'embedding' else 250) + outputs * 1200,
                                       input_tokens=inputs, output_tokens=outputs)
            elif isinstance(usage.get('seconds'), (int, float)) and 0 < usage['seconds'] <= 60:
                self.budget.finish(ticket, round(usage['seconds'] * 75000))
        return response

    def close(self):
        self.inner.close()


def client_options(settings, *, background=False):
    if background:
        return {}
    return {'http_client': httpx.Client(transport=LimitedTransport(settings),
                                       timeout=60, trust_env=False)}
