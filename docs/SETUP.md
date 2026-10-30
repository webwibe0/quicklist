# Установка и воспроизводимость

## Проверенный профиль

Linux x86_64 контейнеры Python 3.13 и Node.js 24 на Docker Desktop/WSL2 в Windows. Базы, app-контейнеры, порты и volumes при публикационном smoke были полностью отдельными; production и личная Windows-БД не использовались. Сборки выполнены с --pull/--no-cache и новой установкой pip/npm. Требуется интернет к package registries. macOS/ARM64 и новая native Windows installation в этой кампании не проверялись.

Сначала выполните [шаги README](../README.md). Для SQLite AI-учёта Compose монтирует каталог, которым владеет непривилегированный пользователь10001. PostgreSQL/backend/frontend публикуются только на loopback. Если порты3000/8000/15432 заняты, измените локальные ports, AUTH_ORIGIN и при native запуске BACKEND_URL согласованно; Next.js rewrites читаются при сборке.

## Внешний обязательный файл

Оригинальный CSV исключён по решению владельца. Получите его у организаторов, не из production backup. [Точное имя, хеш и формат](../data/README.md). PowerShell: `Get-FileHash data/original/voice_assistant_events_1000_77e927b5d2.csv -Algorithm SHA256`; Linux: `sha256sum data/original/voice_assistant_events_1000_77e927b5d2.csv`. Хеш должен совпасть с указанным. Без него runtime завершит инициализацию аккаунта безопасной ошибкой503. Это явно документированная внешняя предпосылка, не скрытый download и не подмена checksum.

Синтетические12строк демонстрируют формат и подходят для ручного importer в отдельной учебной БД. Они не притворяются оригинальными1000 и не проходят его hash gate. Публичный source package сам по себе не содержит данных для полного автоматического seed.

## Что делает локальная миграция

`migrate` использует существующий Alembic и явного начального synthetic legacy owner initial-owner@example.test. Пароль ему не назначается. Зарегистрируйте новый собственный локальный аккаунт через UI. Миграция применяется только к вашей новой БД; перенос существующих данных — отдельная операция, не описанная как автоматический quickstart. При повторном `upgrade head` уже применённые ревизии не выполняются заново.

## Native development — альтернативный вариант

Нужны Python3.13, Node.js24, Docker/Compose и тот же оригинальный CSV. Docker запускает только PostgreSQL; локальные processes используют DATABASE_URL из .env с 127.0.0.1:15432.

```sh
docker compose up -d --wait postgres
python -m venv .venv
```

PowerShell: `.venv\Scripts\Activate.ps1`; Linux/WSL: `source .venv/bin/activate`.

```sh
python -m pip install -c deploy/python.constraints -e "./backend[dev]"
cd backend
alembic -x legacy_owner_confirmed=yes -x legacy_owner_id=20000000-0000-4000-8000-000000000001 -x legacy_owner_email=initial-owner@example.test upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

В другом терминале из frontend: `npm ci`, `npm run build`, `npm start`. В production build backend URL должен быть задан **до** сборки. Native Windows ранее использовался при разработке; новая clean-room проверка этого пакета сделана в Linux-контейнерах, не выдаётся за новый native Windows тест.

## AI и внешние сервисы

Ключи должны принадлежать новой установке. В safe defaults реальные запросы блокируются без ключа и разрешённого бюджета. Для conscious uncapped учёта — AI_USAGE_MODE=accounting_only; capped требует явных allocations, общий AI_TOTAL_BUDGET_USD валидируется до4 в текущем прототипе. Это существующее ограничение capped-профиля, не рекомендация расходов. В production владельца accounting_only уже включён; публичный .env.example его не меняет.

Поддерживаемые профили текущего transport: Luna, gpt-transcribe, gpt-4o-mini-tts, text-embedding-3-large/1536. Unknown binary TTS reserve не считается подтверждённой стоимостью invoice. Не удаляйте ledger для обхода budget/binding. Provider key существует только на backend.

SMTP опционален локально. Public APP_ENVIRONMENT=production требует HTTPS, AUTH_COOKIE_SECURE=true, AUTH_REQUIRE_EMAIL_VERIFICATION=true, работающие MAIL_HOST/MAIL_FROM/SUPPORT_EMAIL и явное открытие регистрации. Не выставляйте local профиль в интернет. Resend потребует своего verified domain, sender и SMTP key; ни один из них не включён в source.

Google: создайте собственный Web OAuth Client и добавьте точный callback из .env. Сохраняйте GOOGLE_TOKEN_KEY постоянно: смена ключа не позволяет расшифровать старые подключения. Raw32byte key кодируется URL-safe base64, OAuth токены шифруются AES-GCM с owner/purpose context. Client Secret не вводится во frontend. Факт подключения не означает включённый export.

## Hybrid Search и worker

FTS/trigram работают сразу, embeddings опциональны. В новой копии нет готовых production vectors. Настройки SEMANTIC_SEARCH_ENABLED/EMBEDDING_REAL_API_ENABLED/EMBEDDING_WORKER_ENABLED выключены. Отдельный worker запускается как `python -m app.worker`, но только после настройки **своего проверенного target** и persistent ledger. Target guard намеренно не является универсальным разрешением индексировать любую URL.

Существующие staging/production profiles ожидают hostname postgres, database quicklist, реальный EMBEDDING_ALLOWED_CLUSTER_ID, marker QUICKLIST_STAGING/QUICKLIST_PRODUCTION,0009,vector0.8.7. Разрешение EMBEDDING_PRODUCTION_INDEXING_APPROVED само по себе не обходит проверку. Обычный quicklist_dev профиль не соответствует этой схеме: не включайте worker вслепую и не подставляйте старые pinned IDs. Подготовка вашей собственной оплачиваемой indexing-среды — отдельное осознанное действие. Lexical fallback и основные функции не требуют worker.

## Тесты

`backend/requirements.lock` — полный pinned set; Docker использует тот же deploy/python.constraints. Для разработки предпочтителен pyproject+constraints, как выше. Тестам нужна собственная writable рабочая копия: mocked provider создаёт временный SQLite-ledger в игнорируемой .local. Read-only source mount без writable copy вызовет безопасный BUDGET_STORAGE_UNAVAILABLE; это не повод отключать limiter.

Из backend: `python -m pytest -q` запускает полный набор, но часть integration modules требует дополнительных изолированных endpoints/marker/cluster и оригинального CSV. Без TEST_DATABASE_URL такие проверки SKIP. Для короткого offline smoke используйте список README. Из корня `python scripts/run_evals.py --offline` использует fake fixture outputs и не оценивает качество живой LLM.

PostgreSQL integration guard проверяет не только название БД: требуется явный loopback disposable endpoint/cluster/marker и vector/schema. Он намеренно откажет произвольной URL. Стенд этапа8.2 ограничен127.0.0.1:55434, именем stage82_*_test и marker QUICKLIST_STAGE82_DISPOSABLE. Полную историческую multi-target test topology эта инструкция не воспроизводит; не выдавайте частично запущенный набор за полный PostgreSQL QA. Для basic clean smoke использовалась отдельная новая БД с собственным marker и реальная local auth; Fake NLU действовал только в отдельном TestClient процессе, без endpoint или server test mode.

Frontend: `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`. Запуск требует node_modules после npm ci; они не включены в Git. Новые результаты и исторические результаты разделены в [аудите](GITHUB-RELEASE-AUDIT.md).

## Troubleshooting

- Login503 при seed: проверьте имя/путь/хеш оригинального CSV внутри build context. После добавления файла пересоберите backend.
- AI unavailable: проверьте собственный ключ, доступный billing, модель и бюджетный режим; health200 не означает оплачиваемый AI.
- Ошибка cookie/CSRF: UI origin должен точно совпадать с AUTH_ORIGIN, не смешивайте localhost и127.0.0.1; local HTTP — только Secure=false.
- Пустая выдача без vector cache не означает отключённый PostgreSQL: используйте точные названия/фрагменты учебного набора и FTS.
- Порт занят: не останавливайте посторонние процессы автоматически, выберите свои свободные порты.

На production не запускайте pytest, smoke scripts, test truncation и dev миграции. Публикация исходников не является разрешением на перенос данных или deployment.
