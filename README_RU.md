# AI Agents Platform

Backend для сохраняемых AI workflow: Django 5.2 LTS, DRF для CRUD, FastAPI для команд исполнения, PostgreSQL и отдельный outbox worker. Требуется Python 3.12 или новее.

Это основа для выполнения графов с аутентификацией, checkpoint и восстановлением задач. Повторная доставка команды не гарантирует однократный внешний эффект: инструменты, которые меняют сторонние системы, должны поддерживать идемпотентность. Показатели задержки, пропускной способности и доступности необходимо измерять в конкретной среде.

[English](README.md) · [Эксплуатация и восстановление](docs/operations.md) · [LLM runtimes](docs/llm-runtimes.md) · [Secret Manager](docs/secret-manager.md) · [GGUF-пилот](docs/cloud-gguf.md) · [Подписки и биллинг](docs/subscriptions-billing.md)

## Локальный запуск

Установите [uv](https://docs.astral.sh/uv/), Docker и Compose:

```sh
git clone https://github.com/Artem7898/ai-agents-platform.git
cd ai-agents-platform
cp .env.example .env
uv sync --frozen --extra dev
docker compose up -d postgres
uv run --frozen python manage.py migrate
uv run --frozen python manage.py createsuperuser
uv run --frozen python manage.py drf_create_token YOUR_USERNAME
```

`uv.lock` фиксирует зависимости и Git-коммит Nova. Реальные ключи и пароли храните вне Git. Для настоящих LLM-запросов задайте `OPENAI_API_KEY` в локальном `.env`; тесты используют моки.

Запустите web и worker в разных терминалах:

```sh
uv run --frozen uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --reload
```

```sh
uv run --frozen python manage.py runworker
```

Без worker команды остаются в очереди. Для проверки блокировок и восстановления с несколькими worker используйте PostgreSQL. SQLite подходит только для упрощённой локальной работы и тестов.

Полный локальный запуск в контейнерах:

```sh
cp .env.example .env
docker compose up --build -d
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py drf_create_token YOUR_USERNAME
```

Compose открывает HTTP и PostgreSQL только на loopback. В нём используются локальные тестовые реквизиты БД; для production нужна отдельная конфигурация.

## Доступ и API

Оба API требуют заголовок `Authorization: Token <ключ>`. Сессия `/admin/` не заменяет API-токен. Обычный пользователь работает со своими агентами и workflow; staff может управлять общими и существующими legacy-объектами. Вне localhost используйте HTTPS.

- `/api/v1/agents/` — CRUD агентов.
- `/api/v1/runtimes/` — настройки LLM runtime: staff управляет ими, владельцы могут читать свои runtime.
- `/api/v1/workflows/` — CRUD определений графов.
- `/api/v1/output-schemas/` — доступный по токену каталог схем структурированных результатов, только для чтения.
- `/api/v1/runs/` — просмотр запусков; состояние меняется отдельными командами.
- `/api/v2/runs/execute` — поставить выполнение в очередь.
- `/api/v2/runs/{run_id}` — получить статус, вход и результат.
- `/api/v2/runs/{run_id}/events?after=0&limit=100` — получить страницу событий.
- `/api/v2/runs/{run_id}/resume` — поставить ответ человека в очередь.

Сначала создайте агента и workflow через CRUD API или Django admin. Пример запуска с UUID существующего workflow:

```sh
curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/execute \
  -H 'Authorization: Token YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"WORKFLOW_UUID","input_data":{"prompt":"Кратко изложи текст"}}'
```

Необязательный заголовок `Idempotency-Key` (1–128 символов) предотвращает повторные команды запуска в своей области действия. Ответ `202 Accepted` подтверждает сохранение команды. Итог исполнения проверяйте отдельно:

```sh
curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/RUN_UUID/events \
  -H 'Authorization: Token YOUR_TOKEN'

curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/RUN_UUID/resume \
  -H 'Authorization: Token YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"input":{"approved":true}}'
```

Если ожидается несколько согласований, передавайте `node_id` рядом с `input` в теле resume. Страница событий содержит `next_after` и `has_more`; максимальный размер страницы — 200.

Точные схемы запросов и ответов доступны в development OpenAPI `/api/v2/docs`. В production документация API отключена.

## Исполнение графов

При проверке определения отклоняются циклы, повторные id и рёбра с неизвестными концами. Узлы выполняются последовательно в порядке зависимостей. Общий потомок запускается один раз после активных предшественников. Согласование человеком сохраняет checkpoint; продолжение проходит через очередь команд.

Условия задаются структурированными значениями, например `{ "path": "decision", "op": "eq", "value": true }`. Python-выражения не исполняются. В workflow можно выбрать `output_node_id` и лимиты `max_steps`, `max_duration_seconds`, `max_tool_calls`, `max_tool_repairs`, `max_output_repairs`, `max_tokens`, `max_output_tokens`, `tool_timeout_seconds`. Точные значения по умолчанию и правила находятся в `src/workflows/schemas.py`. `tool_ids` агента ограничивает допустимые инструменты при вызове.

Весь набор вызовов инструментов проверяется до выполнения первого. Для неверных аргументов предусмотрен ограниченный цикл исправления моделью: счётчик сохраняется при восстановлении, каждая попытка учитывается в бюджете. [Schema-first tools](docs/schema-first-tools.md) описывает `max_tool_repairs`, типизированный контекст исполнения и обработку ошибок.

LLM-узел может выбрать версию серверной схемы и передавать следующим узлам проверенный JSON-объект. [Структурированные результаты workflow](docs/structured-results.md) описывает выбор схемы, сохранение версии, `max_output_repairs` и миграции перед запуском.

Запуск хранит снимок определения workflow, поэтому редактирование исходного графа не меняет уже начатый run. События предназначены для просмотра истории. API произвольного rollback и отмены внешних эффектов нет.

## LLM runtimes

Агент может выбрать управляемый runtime через `runtime_id`: OpenAI, Ollama или OpenAI-совместимый endpoint. Профиль runtime задаёт поддерживаемые возможности запросов и допустимые модели; адреса endpoint и псевдонимы ключей контролирует оператор сервера. Агент без runtime использует прежние глобальные `OPENAI_API_KEY` / `OPENAI_BASE_URL`. [Руководство LLM runtimes](docs/llm-runtimes.md) описывает настройку, права доступа, снимки запусков, проверки совместимости и оставшиеся задачи.

Псевдонимы ключей управляемых runtime могут одновременно использовать Vault KV v2, AWS Secrets Manager и Google Secret Manager. [Secret Manager и ротация](docs/secret-manager.md) описывает серверные привязки, SDK extras, права доступа и обновление ключей по TTL с сохранением активных клиентских запросов.

[Переносимый GGUF-пилот](docs/cloud-gguf.md) описывает отдельный llama.cpp-сервер, проверку файла модели, приватную сеть инференса, TLS, проверки возможностей и измерение стоимости размещения. Облачные ресурсы и веса модели предоставляются отдельно.

## Учёт LLM и квоты

Вызовы workflow учитываются в журнале владельца с квотами запросов и токенов и необязательной оценкой стоимости по версиям тарифов. Staff управляет квотами; неизвестный usage остаётся неразрешённым до сверки оператором. [Учёт LLM и квоты](docs/llm-usage.md) описывает API, резервирование и эксплуатационные ограничения.

[Подписки и платное превышение квоты](docs/subscriptions-billing.md) добавляют версии месячных тарифов, оплаченные календарные периоды, согласованные лимиты дополнительных расходов и внутренние счета. Биллинг по умолчанию выключен; до выбора платёжного провайдера оплату подтверждает staff по проверенным внешним данным.

## Проверки

Для интеграционной проверки нужна PostgreSQL и роль с правом создания тестовой БД:

```sh
uv sync --frozen --all-extras
DJANGO_SETTINGS_MODULE=config.settings.test uv run --frozen --all-extras pytest
uv run --frozen python manage.py makemigrations --check --dry-run
uv run --frozen ruff check .
uv run --frozen pyright
uv build --wheel
uv run --frozen python scripts/wheel_smoke.py
```

Wheel smoke проверяет базовую установку и дополнительные зависимости AWS/GCP в изолированной среде вне checkout. Для него нужен сетевой доступ к пакетам и Git. CI также проверяет миграции и запускает тесты с PostgreSQL 16 со всеми дополнительными зависимостями.

## Production

Для каждого процесса установите `DJANGO_SETTINGS_MODULE=config.settings.prod`. Через менеджер секретов задайте случайный `SECRET_KEY` длиной не менее 50 символов, PostgreSQL `DATABASE_URL`, явный JSON-список `ALLOWED_HOSTS` и реальные ключи LLM. При отсутствии обязательных настроек запуск должен завершаться ошибкой. HTTPS настраивается на reverse proxy; разрешённые frontend origins указываются явно. `TRUST_PROXY_HEADERS=true` допустим только за доверенным proxy, который перезаписывает forwarded-заголовки и закрывает прямой доступ к приложению.

Перед запуском процессов:

```sh
uv run --frozen python manage.py check --deploy
uv run --frozen python manage.py migrate --noinput
uv run --frozen python manage.py collectstatic --noinput
```

Web `uvicorn config.asgi:application` и worker `python manage.py runworker` должны использовать один release и БД, запускаться под supervision. До реальных нагрузок настройте хранение данных, метрики, оповещения и проверенное восстановление backup. Docker image поддерживает те же команды. Для managed-среды с собственным CA можно передать объединённый trusted bundle через BuildKit secret: `docker build --secret id=proxy_ca,src=/path/to/combined-ca.pem .`; сертификат монтируется только при установке зависимостей.

[Runbook](docs/operations.md) описывает миграции, lease, retry, backup и восстановление. Лицензия репозитория — [MIT](LICENSE), зависимости сохраняют собственные лицензии.


Команды используют тот же Token-заголовок. `POST /api/v2/runs/{run_id}/resume` принимает `{"node_id":"approval","input":{"approved":true}}` и ставит продолжение в очередь. При нескольких ожидающих согласованиях укажите узел явно. `POST /api/v2/runs/{run_id}/cancel` запрашивает отмену; выполняющийся инструмент остановится по timeout или на следующей контрольной точке исполнения.

Staff получает агрегаты через `GET /api/v2/operations`: количество запусков и команд по статусам, возраст старейшей pending-команды, просроченные processing leases и сохранённые токены. Endpoint не отдаёт payload и не рассчитывает денежную стоимость. Готовые узлы пока выполняются последовательно в порядке зависимостей.
