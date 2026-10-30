# Шаблоны размещения на собственном сервере

Для локального запуска используйте [README](../README.md) и [SETUP](../docs/SETUP.md). compose.local.yml — localhost-only профиль, не публичный deployment.

deploy/ содержит отдельные generic HTTPS/staging/production templates: Caddy, PostgreSQL, backend, frontend, worker, migration и backup. Они требуют **собственных** секретов и подтверждённой идентичности новой БД. production.env.example не является настройкой работающего VPS. Ни доступа к VPS владельца, ни приватных ключей, ни его volumes или дампов в пакете нет.

Перед своим размещением: настройте HTTPS domain, SMTP/verification, OAuth redirect и encryption key, раздельные постоянные ledgers, backup/restore; подтвердите target cluster/marker/schema, restricted app DB role и внешние порты80/443. PostgreSQL не должен иметь публичный порт. Не отключайте fail-closed assertions мигратора/worker и не подставляйте чужие IDs. Не используйте local tests как migration script.

Сначала проверьте свои templates и restore на отдельной disposable БД. Перенос существующих пользователей и открытие записи требуют отдельного решения владельца данных; здесь нет автоматически выполняемого deployment. Не изменяйте публичный прототип ради клонирования репозитория.

Известные security/compatibility ограничения перечислены в [SECURITY](../docs/SECURITY.md). Публикационный dependency profile отличается от текущего production certifi; не называйте это уже установленным серверным обновлением.
