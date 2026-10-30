# Архитектура QuickList

## Граница доверия

Next.js отвечает за UI, запись/воспроизведение, историю, карточки, сегодняшний planner и формы аккаунта. Запросы /api/v1 проксируются в FastAPI. Ни OpenAI key, ни Google Client Secret, ни пароль PostgreSQL не нужны браузеру.

FastAPI authenticates opaque DB-backed sessions, проверяет Origin/CSRF и привязывает owner_id к SQLAlchemy Session. Session token хранится в HttpOnly cookie, в БД — его hash. Public cookies требуют HTTPS/Secure. Пароли — scrypt с индивидуальной солью. JWT не используется.

## Текст и NLU

1. /assistant/messages загружает только owner-scoped Conversation.
2. DialogueContext содержит сохранённый PendingAction, созданные/возвращённые события и ограниченный контекст.
3. LLMProvider возвращает ParsedCommand через structured output. Модель извлекает намерение и фрагменты, не исполняет SQL и не выбирает произвольный ID.
4. IntentParser проверяет provenance: дата/время/связи должны следовать из текста. Канонические typed tokens разрешают эквивалентные цифровые/словесные записи, сохраняя fail-closed поведение для выдуманных значений.
5. TemporalResolver использует timezone/now и собственную грамматику, вычисляет даты, относительные минуты/часы, интервалы и recurrence. Неполное/неоднозначное время остаётся уточнением, прошлое время требует подтверждения.
6. CommandValidator создаёт отдельную типизированную business command. ActionRouter/UseCase и Repository выбирают разрешённые записи и меняют PostgreSQL.
7. ResponseBuilder возвращает понятное подтверждение, карточки или конкретные missing_fields.

Основные модули: backend/app/ai, temporal, schemas, services/dialogue.py, services/validator.py, use_cases/actions.py, repositories.

## Уточнения и транзакции

PendingAction хранится в JSONB вместе с conversation context. Короткий точный ответ о времени/дате дополняет замороженное действие и не должен заново распознавать intent через LLM. Подтверждение past/duplicate/delete исполняет сохранённую business command, не вычисляет её заново.

services/message_transaction.py освобождает DB transaction до внешнего NLU/query embedding. Затем проверяет неизменность исходного состояния диалога, применяет команду и атомарно сохраняет event/context/messages. Неоднозначное завершение внешнего запроса не маскируется скрытым retry. Это сохраняет БД от половинчатого исполнения и stale context.

## PostgreSQL и ownership

Events содержат UUID, owner_id, тип, title, date/time/end_time, status, source/source_id/type_inferred и timestamps. Conversations и связанные Google данные также owner-scoped. SQLAlchemy ownership hooks и Repository ограничения сохраняются для read/write; произвольный event ID не предоставляет доступ к чужой записи. Изоляция приложения не выдаётся за PostgreSQL row-level security, которой здесь не заявлено.

Миграции находятся в backend/alembic/versions, head0009. Исходные1000 CSV events атомарно инициализируются в аккаунт при первом разрешённом входе. owner+source+source_id обеспечивают идемпотентность; soft-deleted source_id сохраняет факт инициализации. Частичный прежний импорт не восстанавливается вслепую.

## Поиск и индексатор

FTS/trigram выполняются PostgreSQL и ограничивают owner/type/date/status до выдачи. Hybrid добавляет vector candidates из pgvector, объединяет ранги RRF и проверяет явные ограничения guard v2: VERIFIED/REJECTED/UNVERIFIED. Недостаток данных не представляется подтверждённым совпадением. При недоступности query embeddings есть lexical fallback.

EventSearchDocument — projection по canonical title/content_hash; EmbeddingCache содержит model/dimensions/text_version и готовый vector. Worker имеет bounded batch, lease/retry, target identity guard и отдельный постоянный ledger. Seed не ставит автоматически весь CSV в платную очередь; готовые совпадающие vectors можно переиспользовать. Пользовательские vectors/cache не входят в публичный пакет.

## Голос

Browser MediaRecorder → ограниченный бинарный /voice/transcriptions → SpeechProvider/STT → текст в обычный message pipeline. Отдельного привилегированного voice executor нет. Цифры и слова должны проходить одинаковую temporal validation. /voice/speech озвучивает текст через TTS, с auth, лимитом размера, concurrency и учётом. Физический микрофон не заменяется API-тестом.

## Google Calendar и почта

OAuth state одноразовый, привязан к сессии/owner; PKCE и точный redirect URI. Токены шифруются AES-GCM с owner/purpose context. Запрашиваются openid/email/calendar.events.owned. Calendar outbox отделён от локальных транзакций; внешняя доставка не удерживает DB lock. Сохраняется remote Event ID и ревизия, чтобы не создавать дубли при повторной обработке. Connect и sync — разные действия; исходные CSV events не экспортируются автоматически.

SMTP поддерживает STARTTLS587 или implicit TLS465 с проверкой сертификатов. Email verification и reset используют одноразовые expiring tokens, hash в БД и ссылки с fragment, без сырого token в обычных server access logs. Resend — SMTP-провайдер, не новая подсистема аккаунтов.

## AI-учёт и эксплуатация

Интерактивный transport и worker используют разные SQLite-ledger на постоянном volume. capped ограничивает allowances; accounting_only сохраняет записи/резервы и краткосрочные меры защиты, снимая lifetime gates. Это не отменяет биллинг/лимиты OpenAI и не является бесплатным API. Unknown TTS reserve не выдаётся за invoice.

Локально PostgreSQL и приложения работают через Compose/loopback. Server templates включают Caddy, restricted app role, отдельный worker и backup. Эти шаблоны не содержат доступа к production владельца и не выполняют автоматический deploy. [Ограничения и проверки](SECURITY.md), [матрица задания](GORKYCODE-COMPLIANCE.md).
