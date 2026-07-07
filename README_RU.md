# AI Multi-Agent Workflow Platform

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python 3.12">
  <img src="https://img.shields.io/badge/django-5.x-green.svg" alt="Django 5.x">
  <img src="https://img.shields.io/badge/fastapi-latest-teal.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/pydantic-v2-e92063.svg" alt="Pydantic v2">
  <img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="License: MIT">
  <img src="https://img.shields.io/badge/tests-pytest-brightgreen.svg" alt="Tests: pytest">
</p>

<p align="center">
  <b>Продакшн-решение для оркестрации AI-агентов и LLM-воркфлоу: async-first, event-driven backend.</b>
</p>

Построен на **Django 5.x** и **django-nova** с гарантией строгой типизации, zero-downtime деплоев и глубокой наблюдаемости.

---

## 🚀 Ключевые достижения

| Фича | Описание |
|------|----------|
| **Реальный пакет Django Nova** | Используется кастомный пакет `django-nova` с GitHub (не заглушка с PyPI). Реализован паттерн **Single Source of Truth (SSOT)** через строгие Pydantic v2 схемы. |
| **Строгая DDD-архитектура** | Домены разделены на `schemas.py` (Pydantic DTO), `models.py` (ORM State), `services.py` (Business Logic) и `api/` (Controllers). Строгая изоляция слоёв. |
| **Гибридный ASGI-роутер** | В `config/asgi.py` реализовано разделение: **DRF** обрабатывает CRUD (`/api/v1/`), **FastAPI** — Execution/Streaming (`/api/v2/`). |
| **Event Sourcing (Time-Travel)** | В `WorkflowRun` хранятся все шаги в JSONB-массиве `events`. Позволяет откатить состояние запуска на любой шаг. |
| **Async-First Execution** | В `WorkflowExecutor` используется `asyncio.TaskGroup` для конкурентного выполнения независимых веток DAG без оверхеда Celery. |
| **Agent Loop (Tool Calling)** | Реализован бесконечный цикл: LLM просит инструмент → Реестр выполняет → Результат возвращается в LLM → LLM выдаёт финальный ответ. |
| **Строгая Pydantic-валидация** | `extra="forbid"` — запрещаем LLM-галлюцинации на уровне ORM. Невалидный JSON никогда не попадёт в БД. |
| **Connection Pooling** | В `LLMService` используется модульный синглтон `AsyncOpenAI`, сохраняющий TCP-соединения (NFR: p95 first-token < 800ms). |
| **Accepted & Go Pattern** | Контроллер запуска (`POST /api/v2/runs/execute`) возвращает **202 Accepted** мгновенно, а выполнение стартует через `asyncio.create_task`. |
| **Polling API** | `GET /api/v2/runs/{run_id}` возвращает статус и историю событий для UI. |

---

## 📁 Архитектура проекта

Строгая изоляция доменов (DDD) и явное разделение Django (State) и Pydantic (DTO).

```
ai-agents-platform/
├── config/              # Настройки Django (Hybrid ASGI, Uvicorn)
├── src/                 # Исходный код (PYTHONPATH)
│   ├── core/            # Инфраструктура: OpenTelemetry, логирование
│   ├── agents/          # Домен: Агенты (SSOT: AgentSpec)
│   ├── workflows/       # Домен: Воркфлоу (SSOT: WorkflowSpec, NodeKind)
│   ├── runs/            # Домен: Запуски (Event Sourcing: WorkflowRunSpec, EventType)
│   └── api/             # Слой представления (Controllers)
│       ├── drf/         # DRF ViewSets (через to_drf_serializer)
│       └── fastapi/     # FastAPI Routers (Execution, Polling)
└── tests/               # Тесты (pytest + pytest-asyncio + respx)
    ├── unit/            # Тесты схем, tool_registry (без БД)
    ├── integration/     # Тесты сервисов, API (с тестовой БД)
    └── e2e/             # Тесты API и LLM-интеграции (через моки)
```

---

## 🚀 Быстрый старт

### 1. Клонирование и настройка

```bash
git clone https://github.com/Artem7898/ai-agents-platform
cd ai-agents-platform
```

### 2. Создание окружения и установка зависимостей (через `uv`)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

> ⚠️ **Важно:** Пакет `django-nova` берётся напрямую из GitHub https://github.com/Artem7898/django-nova (на PyPI лежит битая заглушка).
> Если вы используете свой форк `django-nova`, измените строку в `pyproject.toml`:
> ```toml
> "https://github.com/Artem7898/django-nova"
> ```

---

## ▶️ Как запустить (Development)

Для локальной разработки используем **SQLite** (чтобы не поднимать Docker) и **Uvicorn** для поддержки гибридного ASGI.

```bash
# Применить миграции и запустить сервер
python manage.py migrate
uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload
```

### 🐳 Альтернатива: Production (через реальную БД)

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev_postgres   uvicorn config.asgi:application --host 0.0.0.0 --port 8000
```

---

## 🧪 Как запускать тесты

В проекте используется **pytest** с плагинами `pytest-asyncio` и `respx`.

```bash
# Все тесты (Unit + Integration + E2E)
pytest tests/ -v --cov=src --cov-report=term-missing

# Только быстрые Unit-тесты (без поднятия БД)
pytest tests/unit/ -v

# Только интеграционные тесты (с тестовой БД)
pytest tests/integration/ -v --reuse-db
```

---

## 🐛 Решённые проблемы (Почему это было сложно)

| Проблема | Решение |
|----------|---------|
| **Конфликт лейблов (`duplicates: agents`)** | Пакет `django-nova` содержит внутренний модуль с лейблом `agents`. Решено заданием `label = 'domain_agents'` в `src/agents/apps.py`. |
| **`EnumType.__call__() missing 1 argument`** | Django 5.0 не принимает чистый Python `StrEnum` в параметр `choices`. Решено генерацией списка кортежей: `[(s.value, s.name) for s in AgentStatus]`. |
| **`cache_ttl` вместо `cache_ttl_seconds`** | В пакете использовался `cache_ttl`, а в коде фреймворка параметр называется `cache_ttl_seconds`. Имена параметров выровнены. |
| **`AppRegistryNotReady`** | Импорт `NovaModel` внутри `__init__.py` ломал инициализацию Django. Решено переносом импортов строго в `models.py`. |
| **SQLite `ArrayField` в тестах** | SQLite не поддерживает массивы. Решено переводом поля `tool_ids` на `models.JSONField`, так как Pydantic всё равно гарантирует типизацию. |

---

## 🛠 Что делать дальше (Roadmap)

- [ ] **Outbox Pattern** — Заменить `asyncio.create_task()` на запись в таблицу `outbox`. Позволит перезапускать сервер без потери фоновых задач.
- [ ] **Real LLM Streaming** — Интегрировать `httpx`-стриминг в `LLMService` для вывода токенов в реальном времени.
- [ ] **Human-in-the-loop** — Реализовать приостановку графа, сохранение контекста в БД и эндпоинт `/resume` для продолжения выполнения.
- [ ] **Time-Travel UI** — Frontend запрашивает массив `events` из `WorkflowRun` и отрисовывает временную шкалу выполнения графа.

---

## 🔄 Схема выполнения



---

## 📡 Примеры API

```bash
# Запуск выполнения (возвращается мгновенно)
POST /api/v2/runs/execute
# → 202 Accepted { "run_id": "uuid", "status": "PENDING" }

# Получение статуса и событий
GET /api/v2/runs/{run_id}
# → { "status": "RUNNING", "events": [...] }
```

---

## 📄 Лицензия

MIT © [Artem Alimpiev ](https://github.com/Artem7898)
