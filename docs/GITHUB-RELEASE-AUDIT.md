# GitHub publication audit —30.10.2026

**CONDITIONAL READY**. Публикацию выполняет владелец; git push, remote repository, Codeberg/GitHub upload и VPS deployment не выполнялись.

| Пункт | Результат | Доказательство / граница |
|---|---|---|
| Фактический production | backend cf3f09b0ffe339c8; frontend1xrOHW4t65_LIcJDgw75r; Alembic0009 | Свежий read-only fingerprint, без чтения пользовательских текстов |
| Исходный пакет | тот же app fingerprint, публикационный dependency profile certifi2026.10.19 | Новая сборка, не новый установленный VPS release |
| Число файлов | 268 | Manifest охватывает все файлы, кроме самого manifest; ZIP проверяется отдельно |
| Source parity | PASS для67 app файлов и frontend sources; head0009 | Byte SHA-256 против фактического production; старые LF/CRLF сохранены |
| Docker image parity | Объяснённое отличие | Backend dependencies исправлены; images пересобраны; .next не публикуется, build ID новой сборки отличается |
| README | PASS | Собственные prerequisites/команды, paid AI отдельно от startup, dataset requirement виден до запуска |
| Architecture | PASS | LLM/data/temporal/transactions/auth/voice/search/calendar/worker разграничены |
| GORKYCODE | Материалы PASS; продуктовая приёмка историческая CONDITIONAL PASS |69 строк исходного аудита,12 точных фраз, ранее448/46/2; новый полный QA не запускался |
| Clean setup | PASS **при отдельно предоставленном оригинальном CSV** | Новая папка, новая установка deps, отдельный PG17.11/vector0.8.7,0001→0009, startup; без исходного CSV полный account setup не работает |
| Basic clean smoke | PASS | Подлинные local registration/login/CSRF/ownership, CREATE/LIST/lexical SEARCH, SQL persistence,1000 personal seeds и no duplicate relogin; fake NLU только отдельного TestClient процесса, paid calls0 |
| Backend build/tests | PASS | Чистый Docker build;167 целевых offline tests; Ruff PASS. Первый запуск на read-only source mount дал9 storage errors, повторён без изменения assertions в собственной writable copy и прошёл |
| Frontend build/tests | PASS | npm ci, production build,90 tests, TypeScript, ESLint; runtime исходники не менялись |
| Dependency audit | PARTIAL | npm audit0; certifi critical устранён и повторно проверен;4 unique advisories cryptography остаются в недостижимых здесь X.509/PKCS7/wheel OpenSSL путях; подробности SECURITY |
| Secret scan | PASS с оговоркой границ | Локальные patterns+entropy+ручная классификация, весь source и финальный ZIP; значения секретов не выводились; Gitleaks не установлен |
| Personal data | PASS | Нет production DB/events/dialogues/ledgers/vectors/credentials/session dumps; публичный contact уже разрешён владельцем |
| Original CSV | EXCLUDED по решению владельца | Redistribution permission не подтверждена; в ZIP только12 новых synthetic rows + инструкция получения и SHA оригинала |
| Env examples | PASS |63 фактически распознанных Settings/Compose/frontend переменных; local HTTP auth согласован; optional SMTP/Google/worker выключены; ключи пусты |
| Docker/Compose | PASS local config/build | Нормальные pinned base images, no secrets in build args/layers, loopback ports, новые volumes; dockerignore исправлен для локально предоставленных данных |
| Git history | EXCLUDED | Нет .git; рабочая история владельца не менялась; ignore проверен15 приватных и9 source/example кейсов, attrs сохраняет байты |
| Архив | PASS | CRC, path traversal, byte SHA268 файлов, повторная распаковка/scan;58 ключевых offline PASS из распакованного source; ZIP SHA — отдельный sidecar |
| Known limitations | PRESENT | README/SETUP/SECURITY/compliance, Google warning, recurrence/пагинация/browser gap, model/billing/worker target guard |

## Отличия от production и полнота

Runtime app не изменён. Намеренные изменения публикации: безопасные gitignore/gitattributes/env examples; полный requirements lock; разрешённый data build context; compose.local overlay; self-contained документация; исключение неиспользуемых executable rehearsal scripts и приватных исторических отчётов; исключение оригинального CSV; certifi2026.10.19 в constraints.

Новый backend image: `sha256:a7293e0b55ca059b8d318be51e4adce164d1c4d65043fb1f72fa0f510f749c4f`. Новый frontend image: `sha256:6578af60af698a23241fab33f15ebfa9fbb407f55c2b768e3078383ca53e96d6`, новый локальный build ID: `29qZGcd1K2y1rAHLDjTVG`. Случайный Next build ID при пересборке не является source mismatch. Production остаётся на своём ранее указанном image/build.

Backend/frontend/API/LLM/temporal/pending/business/storage/migrations/search/worker/STT/TTS/Google/auth/verification/reset/ownership/importer/seeding/tests/deploy присутствуют. CI не добавлялся: нет нового auto-deploy и платных workflow. Некоторые integration tests исторически требуют pinned multi-target disposable topology; это не выдаётся за portable full QA на любой URL. Все исполняемые команды чистого запуска проверены по существующим файлам; Compose overlay отдельно валидирован, networking/ports проверены на isolated стенде с другими свободными loopback портами.

## Security findings и действия владельца

Действующих production secrets в пакете не обнаружено. Ротация секретов не выполнялась и не объявляется необходимой без доказательства утечки. По подтверждённому advisory старого certifi требуется отдельно согласовать обновление production dependency profile; данный пакет его на VPS не устанавливал. Критический defect публикационной сборки устранён, остаточные cryptography warnings не скрыты. Полный OS vulnerability scan не проводился.

Лицензия пока не выбрана — LICENSE отсутствует по решению владельца. Оригинальный dataset получайте у организаторов, а не из чужой БД. Перед публикацией изучите [guide](../GITHUB-PUBLISH-GUIDE.md), убедитесь в Public visibility и актуальном README, добавьте фактическую ссылку repository после самостоятельной публикации.

## SHA-256 и граница поставки

Хеш ZIP поставляется **рядом с архивом** в .zip.sha256 и во внешней окончательной копии этого аудита. Он не встраивается в файл внутри того же ZIP: иначе образуется циклическое самохеширование. RELEASE_MANIFEST.json описывает содержимое папки и file hashes без самоссылки. Ни .env чистого стенда, ни его оригинальный CSV, ни локальные scan/build logs в поставку не входят.

[Архитектура](ARCHITECTURE.md) · [Compliance](GORKYCODE-COMPLIANCE.md) · [Security findings и официальные advisory](SECURITY.md) · [Setup](SETUP.md).

Дополнительный browser bundle scan:27 статических файлов,0 известных secret patterns; baked secret ENV в новых images отсутствует. После распаковки source ZIP58 targeted offline PASS. Финальное изменение ZIP касается только этого отчёта и manifest; app/test bytes повторно сверены с проверенным распакованным source.
