# Безопасность публичного пакета

## Что проверено

Пакет собран из версии backend cf3f09b0ffe339c8, побайтово сравнен с67 production app-файлами и утверждёнными frontend sources. Пользовательские базы, диалоги, vectors, OAuth credentials, сессии, OpenAI/SMTP/SSH keys, AI-ledgers, backups, private .env и старая .git history не включены. Оригинальный CSV исключён по решению владельца. Единственный публичный support email, уже присутствующий на страницах приложения, ранее явно разрешён владельцем; отрицательный тест неактивного адреса не делает его рабочим контактом.

Gitleaks/git-secrets не были установлены. Выполнен локальный pattern/entropy/content scan всех включённых файлов и повторный scan ZIP; результаты проверены вручную без передачи возможных секретов во внешние сервисы. Checksums, package integrity, явно синтетические тестовые строки и генерируемые ephemeral keys не приравниваются к production credentials. Такой scan не является математическим доказательством отсутствия любых секретов.

`.gitignore` защищает env/credentials/databases/keys/backups/builds/caches/архивы; examples остаются включаемыми. `.gitattributes` сохраняет проверенные байты: runtime имеет смешанные LF/CRLF, и автоматическое преобразование изменило бы source fingerprint. Игнорирование не заменяет аудит уже committed файлов. Старая Git-история не переносится.

## Целевой аудит кода

| Область | Реальная защита / граница |
|---|---|
| Auth | Случайные сессии, hash token в БД, scrypt; нет JWT/admin hardcoded key |
| CSRF/CORS | Проверки Origin и CSRF на mutations, SameSite cookies; wildcard CORS не включён |
| Public cookies | HTTPS/Secure/email verification проверяются Settings для public profiles |
| Ownership | Session binding, owner-filtered queries, foreign IDs недоступны; не заявляется RLS |
| SQL | SQLAlchemy bound values; LLM SQL не выполняет; нет public TRUNCATE endpoint |
| Ошибки/health | Production скрывает model/version; safe error messages; validation не отражает input secrets |
| OAuth | State/PKCE/session owner/replay checks; exact callback; encrypted token context |
| Voice | Auth, ограниченный размер/время, concurrency; аудио не пишется в произвольный путь |
| Логи | OAuth query redaction, без response/secret traceback в provider diagnostics, disabled access logs в deploy |
| Frontend | API keys не используются как NEXT_PUBLIC; browser bundle scan отдельно |
| Telemetry | NEXT_TELEMETRY_DISABLED=1 при Docker build; нет добавленного стороннего analytics collector |
| Test tools | Fake providers используются только тестами; нет включаемого публичного dev-bypass endpoint |

Исторические offline и auth/ownership regressions сохранены. Новая clean-room регистрация/вход/CSRF и CREATE/LIST/SEARCH проверены на fresh disposable DB, не на production. Это целевой обзор, не внешний penetration test и не новый полный Stage QA.

## Зависимости — фактический остаток

Npm audit по полному lockfile вернул0 advisories. Python audit нашёл5 уникальных advisory IDs в2 packages (часть записей сканера дублировалась). Критическое предупреждение certifi GHSA-6q48-6c7w-9gw2 устранено **только в публикационном профиле**: 2026.7.22 →2026.10.19. Повторный pip-audit не показывает certifi finding. Runtime source fingerprint не меняется; новый Docker image закономерно отличается от production dependencies. Production автоматически не обновлялся.

Остаются4 уникальных предупреждения cryptography46.0.7: PYSEC-2026-3552,3553,3554 и GHSA-537c-gmf6-5ccf. Они относятся к PKCS7 decryption, X.509 verification и bundled OpenSSL. QuickList использует только AESGCM для OAuth и Fernet для archive encryption; входные PKCS7/X.509 chains не обрабатывает и pyca как TLS verifier не применяет. Это подтверждено поиском импортов/call sites. На основании текущих путей эти advisories не считаются доказанной уязвимостью API QuickList, но **не удалены из отчёта**. Major update cryptography до48/49/50 не делался ради зелёного сканера; нужен отдельный совместимый dependency review при его обновлении.

Источники: [certifi advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2), [cryptography wheels](https://github.com/pyca/cryptography/security/advisories/GHSA-537c-gmf6-5ccf), [X.509 wildcard](https://github.com/pyca/cryptography/security/advisories/GHSA-m2h6-j472-rp4c), [OpenSSL June2026](https://openssl-library.org/news/secadv/20260609.txt). Остаток помечен PARTIAL, а не «все зависимости безопасны».

Base images закреплены digest; повторный полный OS CVE scan средствами Trivy/Scout не проводился, такие средства не были установлены. pip-audit не может проверить локальный проект gorkycode-text-core по PyPI: его код проверялся отдельно. Невыявленная утечка/эксплуатация не предполагается, ротация credentials без доказательства не выполнялась.

## Для владельца новой установки

Используйте собственные secrets; не включайте local HTTP profile в интернет. Не подключайте disposable tests к рабочей БД. Не удаляйте ledger и не копируйте production data в Git. Для public SMTP/Google нужны собственные verified настройки, для paid AI — самостоятельное согласие на расходы. [.env.example](../.env.example) — только local template.

Production владельца сохранил прежний certifi: исправленный dependency profile этого ZIP требует отдельного controlled installation для VPS. Публикация исходников сама по себе не устанавливает исправление на сервер. Нет автоматического CI deploy и новых GitHub tokens.
