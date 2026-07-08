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
  <b>Production-ready, async-first, event-driven backend for orchestrating AI agents and LLM workflows.</b>
</p>

Built on **Django 5.x** and **django-nova**, ensuring strict typing, zero-downtime deployments, and deep observability.

---

## 🚀 Key Achievements

| Feature | Description                                                                                                                                                                                          |
|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Real Django Nova Package** | Uses a custom `django-nova` package from GitHub https://github.com/Artem7898/django-nova (not a PyPI stub). Implements the **Single Source of Truth (SSOT)** pattern via strict Pydantic v2 schemas. |
| **Strict DDD Architecture** | Domains are separated into `schemas.py` (Pydantic DTOs), `models.py` (ORM State), `services.py` (Business Logic), and `api/` (Controllers). Strict layer isolation.                                  |
| **Hybrid ASGI Router** | `config/asgi.py` implements routing: **DRF** handles CRUD (`/api/v1/`), **FastAPI** handles Execution/Streaming (`/api/v2/`).                                                                        |
| **Event Sourcing (Time-Travel)** | `WorkflowRun` stores all steps in a JSONB `events` array. Allows rolling back execution state to any step.                                                                                           |
| **Async-First Execution** | `WorkflowExecutor` uses `asyncio.TaskGroup` for concurrent execution of independent DAG branches without Celery overhead.                                                                            |
| **Agent Loop (Tool Calling)** | Infinite loop implemented: LLM requests tool → Registry executes → Result returned to LLM → LLM produces final answer.                                                                               |
| **Strict Pydantic Validation** | `extra="forbid"` — prevents LLM hallucinations at the ORM level. Invalid JSON never reaches the database.                                                                                            |
| **Connection Pooling** | Modular singleton `AsyncOpenAI` in `LLMService`, preserving TCP connections (NFR: p95 first-token < 800ms).                                                                                          |
| **Accepted & Go Pattern** | Execution controller (`POST /api/v2/runs/execute`) returns **202 Accepted** instantly, while execution starts via `asyncio.create_task`.                                                             |
| **Polling API** | `GET /api/v2/runs/{run_id}` returns status and event history for the UI.                                                                                                                             |

---

## 📁 Project Architecture

Strict domain isolation (DDD) and explicit separation of Django (State) and Pydantic (DTO).

```
ai-agents-platform/
├── config/              # Django settings (Hybrid ASGI, Uvicorn)
├── src/                 # Source code (PYTHONPATH)
│   ├── core/            # Infrastructure: OpenTelemetry, logging
│   ├── agents/          # Domain: Agents (SSOT: AgentSpec)
│   ├── workflows/       # Domain: Workflows (SSOT: WorkflowSpec, NodeKind)
│   ├── runs/            # Domain: Runs (Event Sourcing: WorkflowRunSpec, EventType)
│   └── api/             # Presentation layer (Controllers)
│       ├── drf/         # DRF ViewSets (via to_drf_serializer)
│       └── fastapi/     # FastAPI Routers (Execution, Polling)
└── tests/               # Tests (pytest + pytest-asyncio + respx)
    ├── unit/            # Schema tests, tool_registry (no DB)
    ├── integration/     # Service tests, API tests (with test DB)
    └── e2e/             # API and LLM integration tests (via mocks)
```

---

## 🚀 Quick Start

### 1. Clone and setup

```bash
git clone https://github.com/your-org/ai-agents-platform.git
cd ai-agents-platform
```

### 2. Create environment and install dependencies (via `uv`)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

> ⚠️ **Important:** The `django-nova` package is pulled directly from GitHub (the PyPI version is a broken stub).
> If you are using your own fork of `django-nova`, change the line in `pyproject.toml` to:
> ```toml
> "django-nova @ git+https://github.com/<your-username>/<your-fork>.git"
> ```

---

## ▶️ How to Run (Development)

For local development, we use **SQLite** (to avoid Docker setup) and **Uvicorn** for hybrid ASGI support.

```bash
# Apply migrations and start the server
python manage.py migrate
uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --reload
```

### 🐳 Alternative: Production (with real database)

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev_postgres   uvicorn config.asgi:application --host 0.0.0.0 --port 8000
```

---

## 🧪 How to Run Tests

The project uses **pytest** with plugins `pytest-asyncio` and `respx`.

```bash
# All tests (Unit + Integration + E2E)
pytest tests/ -v --cov=src --cov-report=term-missing

# Only fast Unit tests (no DB required)
pytest tests/unit/ -v

# Only Integration tests (with test DB)
pytest tests/integration/ -v --reuse-db
```

---

## 🐛 Solved Problems (Why This Was Hard)

| Problem | Solution |
|---------|----------|
| **Label conflict (`duplicates: agents`)** | The `django-nova` package contains an internal module with the `agents` label. Fixed by setting `label = 'domain_agents'` in `src/agents/apps.py`. |
| **`EnumType.__call__() missing 1 argument`** | Django 5.0 does not accept pure Python `StrEnum` in the `choices` parameter. Fixed by generating a tuple list on the fly: `[(s.value, s.name) for s in AgentStatus]`. |
| **`cache_ttl` instead of `cache_ttl_seconds`** | The package used `cache_ttl`, but the framework code uses `cache_ttl_seconds`. Parameter names aligned. |
| **`AppRegistryNotReady`** | Importing `NovaModel` inside `__init__.py` broke Django initialization. Fixed by moving imports strictly to `models.py`. |
| **SQLite `ArrayField` in tests** | SQLite does not support arrays. Fixed by migrating `tool_ids` to `models.JSONField`, since Pydantic guarantees typing anyway. |

---

## 🛠 Roadmap

- [ ] **Outbox Pattern** — Replace `asyncio.create_task()` with writes to an `outbox` table. This allows server restarts without losing background tasks.
- [ ] **Real LLM Streaming** — Integrate `httpx` streaming into `LLMService` for real-time token output.
- [ ] **Human-in-the-loop** — Implement graph pause, context persistence in DB, and a `/resume` endpoint to continue execution.
- [ ] **Time-Travel UI** — Frontend can request the `events` array from `WorkflowRun` and render a timeline of graph execution.

---

## 🔄 Execution Flow

```mermaid
graph TD
    Client(Frontend) --> Uvicorn[Hybrid ASGI]

    subgraph Uvicorn[API Gateway]
        direction LR

        subgraph Django_DRF [Synchronous CRUD]
            DRF_URL["/api/v1/"]
            DRF_URL --> ViewSets(AgentViewSet)
            ViewSets --> to_drf_serializer
            to_drf_serializer --> to_django_orm
        end

        subgraph FastAPI_Async [Async Execution & SSE]
            FASTAPI_URL["/api/v2/"]
            FASTAPI_URL --> RunController[/api/v2/runs/execute]
            RunController -->|Return 202 Accepted & Run ID| Client
            RunController --> asyncio.create_task(_run_background)

            subgraph BackgroundWorker [Event Sourcing & DAG Execution]
                BackgroundWorker --> WorkflowExecutor
                WorkflowExecutor --> aget(Run)
                WorkflowExecutor --> TaskGroup[_execute_node]

                subgraph DAG_Execution [Async Parallelism]
                    TaskGroup --> LLMService[Agent Loop: Tool Calling]
                    LLMService --> AsyncOpenAI
                    AsyncOpenAI --> LLMService
                    LLMService -- "Call tool" --> TaskGroup
                    TaskGroup --> ToolRegistry.execute()
                    ToolRegistry --> ToolRegistry._executors
                    ToolRegistry -- "Return result to context" --> LLMService
                    LLMService -- "Final answer" --> TaskGroup
                end

                WorkflowExecutor --> aupdate_fields[status: COMPLETED]
                WorkflowExecutor --> _append_event[Event: NODE_EXIT]
            end
        end
    end
```

---

## 📡 API Examples

```bash
# Start execution (returns immediately)
POST /api/v2/runs/execute
# → 202 Accepted { "run_id": "uuid", "status": "PENDING" }

# Poll for status and events
GET /api/v2/runs/{run_id}
# → { "status": "RUNNING", "events": [...] }
```

---

## 📄 License

MIT © [your-org](https://github.com/your-org)