# Ручная публикация QuickList владельцем

Никакие действия ниже агент не выполнял. Используйте **только чистую папку quicklist-gorkycode**, не соседние clean-room, отчёты с logs, исходную рабочую директорию, production backups или ZIP с частными материалами. LICENSE пока не выбран. Оригинального CSV нет по вашему решению; не добавляйте его без разрешения организаторов.

## Способ1 — GitHub Web UI, без установки Git

1. Самостоятельно войдите/создайте аккаунт на GitHub и завершите его security setup.
2. New repository → имя, например quicklist-gorkycode → Public. Описание: «Русскоязычный AI-ассистент для задач, встреч и напоминаний — GORKYCODE2026». Веб-сайт: https://quicklist-ai.com.
3. Не создавайте автоматически новый README/.gitignore/LICENSE: они уже подготовлены, лицензия не выбрана. Создайте пустой репозиторий.
4. Add file → Upload files. Загружайте **содержимое** папки, чтобы README.md, backend/, frontend/, docs/ оказались в корне. ZIP не должен быть единственным файлом репозитория.
5. GitHub Web UI принимает до100 файлов за одну загрузку; у пакета больше100. Загружайте по каталогам/партиями до100, сохраняя структуру. Для точного переноса скрытых файлов удобнее GitHub Desktop. Проверьте, что .gitignore, .gitattributes, .env.example и Docker configs действительно добавлены: браузер может не показать скрытые файлы в диалоге выбора.
6. Перед каждым commit просмотрите список. Не добавляйте .env, оригинальный CSV, secrets/, node_modules, .next, .local, ledgers, dumps, screenshots чужих данных. При secret protection warning остановитесь и устраните причину; не обходите его ради публикации.
7. Откройте главную репозитория без входа (private/incognito window). README должен отображаться; проверьте backend/app, frontend/app, migrations, tests, docs. Скопируйте фактический URL и добавьте его в README и форму GORKYCODE.

## Способ2 — GitHub Desktop или Git

GitHub Desktop можно установить самостоятельно с официального сайта. Создайте локальный репозиторий **в новой чистой папке пакета**, без переноса старой .git. Проверьте Changes: только предусмотренные исходники и documentation, без оригинального CSV и env. Commit → Publish repository → отключите Keep this code private для публичной сдачи. Не выбирайте автоматическую лицензию вместо решения владельца.

Если предпочитаете Git, выполните самостоятельно из чистой папки:

```sh
git init
git branch -M main
git add .
git status --short
git diff --cached --stat
git commit -m "QuickList GORKYCODE 2026 publication package"
```

После создания **своего** пустого public repository скопируйте его фактический URL из GitHub, выполните `git remote add origin <ФАКТИЧЕСКИЙ_URL>` и `git push -u origin main`. Угловой placeholder заменяется, это не выдуманный адрес. Аутентификацию выполните самостоятельно; пароль/токен не передавайте в чат и не сохраняйте в remote URL. .gitattributes сохраняет исходные байты и смешанные LF/CRLF; не форматируйте весь backend перед публикацией.

## После загрузки

- Проверьте Public visibility и доступ без аккаунта, README, относительные ссылки, structure и реальный прототип.
- Settings → Security/Code security (названия могут зависеть от интерфейса): проверьте доступные Secret scanning, Push protection, Dependency graph и Dependabot alerts. Не предоставляйте широкие token permissions ради этих функций. Не обходите обнаружение секретов.
- Security alerts не должны заменять локальную проверку; известные остаточные dependency findings описаны в docs/SECURITY.md.
- Actions в этом пакете не добавлены: нет автоматического deployment/платных API calls. Не подключайте production secrets к pull requests извне.
- Решение о лицензии можете принять позднее отдельным commit; не присваивайте себе права на сторонний датасет/зависимости.
- В Website/about укажите https://quicklist-ai.com; ссылку репозитория отправьте организаторам. Убедитесь, что README честно объясняет получение отсутствующего оригинального CSV и платные AI prerequisites.

Официальные инструкции: [создание репозитория](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository), [Web upload и ограничения](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository), [secret scanning](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning), [доступность security features](https://docs.github.com/en/get-started/learning-about-github/about-github-advanced-security). Проверены30.10.2026. Никакого git push или создания удалённого repository агент не выполнял.
