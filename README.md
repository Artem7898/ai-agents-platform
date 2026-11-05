# AI Agents Platform

A Django 5.2 LTS backend for persisted AI workflows, with Django REST Framework for CRUD, FastAPI for execution commands, PostgreSQL, and a separate outbox worker. Python 3.12 or newer is required.

The project provides a foundation for authenticated workflow execution, graph validation, checkpoints and recovery. External tool effects need their own idempotency guarantees: retrying a message cannot guarantee exactly-once effects in another system. Latency, throughput and availability depend on the deployment and must be measured there.

[Русская инструкция](README_RU.md) · [Browser console](docs/console.md) · [Operations and recovery](docs/operations.md) · [LLM runtimes](docs/llm-runtimes.md) · [Secret Manager](docs/secret-manager.md) · [GGUF pilot](docs/cloud-gguf.md) · [Subscriptions and billing](docs/subscriptions-billing.md)

The [AgentOps console](docs/console.md) at `/console/` adds a session-authenticated dashboard, agent/workflow management, run inspection and billing views. Build the frontend assets before serving the console, or use the application Docker build. The existing token APIs retain their authentication rules.

## Local setup

Install [uv](https://docs.astral.sh/uv/), Node.js 24 and Docker with Compose, then:

```sh
git clone https://github.com/Artem7898/ai-agents-platform.git
cd ai-agents-platform
cp .env.example .env
uv sync --frozen --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
docker compose up -d postgres
uv run --frozen python manage.py migrate
uv run --frozen python manage.py createsuperuser
uv run --frozen python manage.py drf_create_token YOUR_USERNAME
```

The checked-in lock file fixes the runtime dependency graph, including the Nova Git revision. Keep real API keys and database credentials outside version control. Add `OPENAI_API_KEY` to your local `.env` only when using a real LLM; tests use mocks.

Start the web process and worker in separate terminals:

```sh
uv run --frozen uvicorn config.asgi:application --host 127.0.0.1 --port 8000 --reload
```

```sh
uv run --frozen python manage.py runworker
```

A web process alone accepts commands but does not consume the queue. PostgreSQL is required for the multi-worker locking and recovery guarantees; SQLite is only a lightweight local/test option.

Open `http://127.0.0.1:8000/console/` and sign in with the Django username and password. The API token created above is for the Token APIs and is not needed by the browser console.

Alternatively, run the application and its database in containers:

```sh
cp .env.example .env
docker compose up --build -d
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py drf_create_token YOUR_USERNAME
```

Compose binds PostgreSQL and HTTP to loopback and uses development-only database credentials. It is a local setup, not a production deployment manifest.

## Authentication and API

Both API versions require `Authorization: Token <key>`. Django sessions are used for `/admin/`; logging into the admin does not replace the API token. Ordinary users see their owned agents and workflows. Staff can manage shared/legacy records. Treat tokens as passwords and use HTTPS outside localhost.

- `/api/v1/agents/`: agent CRUD.
- `/api/v1/runtimes/`: staff-managed LLM runtime configuration; owners can read their runtimes.
- `/api/v1/workflows/`: workflow CRUD.
- `/api/v1/output-schemas/`: authenticated read-only catalog of structured result contracts.
- `/api/v1/runs/`: run inspection; state changes use explicit commands.
- `/api/v2/runs/execute`: queue execution.
- `/api/v2/runs/{run_id}`: inspect status, input and output.
- `/api/v2/runs/{run_id}/events?after=0&limit=100`: inspect paginated persisted events.
- `/api/v2/runs/{run_id}/resume`: queue human input for a waiting run.

Create an agent and workflow through the CRUD API or Django admin first. Use an actual workflow UUID below:

```sh
curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/execute \
  -H 'Authorization: Token YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"WORKFLOW_UUID","input_data":{"prompt":"Summarize this text"}}'
```

An optional `Idempotency-Key` header (1–128 characters) prevents duplicate execution commands within its scope. Execution commands return `202 Accepted` with the run identifier. Acknowledgement means the command is persisted; inspect the run to learn whether execution completed:

```sh
curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/RUN_UUID/events \
  -H 'Authorization: Token YOUR_TOKEN'

curl --fail-with-body http://127.0.0.1:8000/api/v2/runs/RUN_UUID/resume \
  -H 'Authorization: Token YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"input":{"approved":true}}'
```

For multiple pending approvals, include `node_id` beside `input` in the resume body. Event pages return `next_after` and `has_more`; the maximum page size is 200.

Use the generated development OpenAPI documentation at `/api/v2/docs` for exact request/response schemas. Documentation endpoints are disabled in production.

## Workflow behavior

Definitions are validated before execution: node identifiers must be unique, edge endpoints must exist, and cycles are rejected. Nodes execute in dependency order; a shared successor executes once after its active predecessors. Node execution is currently sequential. Human approval checkpoints preserve the pending graph state and resume through the same durable command path.

Conditions use structured values such as `{ "path": "decision", "op": "eq", "value": true }` rather than executable Python expressions. A workflow can select `output_node_id` and configure limits including `max_steps`, `max_duration_seconds`, `max_tool_calls`, `max_tool_repairs`, `max_output_repairs`, `max_tokens`, `max_output_tokens` and `tool_timeout_seconds`. See the schemas in `src/workflows/schemas.py` for defaults and validation rules. Agent `tool_ids` are an execution allowlist, not merely suggestions to the model.

Tool batches are validated before execution. Invalid arguments can request a bounded model correction, with counters preserved across recovery and each attempt charged to the normal usage budget. [Schema-first tools](docs/schema-first-tools.md) covers `max_tool_repairs`, typed execution context and failure behavior.

LLM nodes can select a versioned server output schema and return validated JSON objects to downstream nodes. [Structured workflow results](docs/structured-results.md) covers contract selection, frozen versions, `max_output_repairs` and deployment migrations.

Runs preserve a snapshot of the workflow definition so editing a workflow does not change an existing run. Persisted events support inspection; they do not provide an API for undoing an external side effect or arbitrary time-travel rollback.

## LLM runtimes

An agent can select a managed runtime through `runtime_id`: OpenAI, Ollama, or an OpenAI-compatible endpoint. Runtime profiles declare supported request features and allowed models; endpoint URLs and credential aliases are controlled by the server operator. Agents without a runtime keep the global `OPENAI_API_KEY` / `OPENAI_BASE_URL` configuration. See [LLM runtimes](docs/llm-runtimes.md) for setup, access controls, run snapshots, compatibility testing and follow-up work.

Managed credential aliases can use Vault KV v2, AWS Secrets Manager and Google Secret Manager concurrently. [Secret Manager and rotation](docs/secret-manager.md) covers server bindings, SDK extras, access policies and TTL-bound key rotation without interrupting active client leases.

[Portable GGUF pilot](docs/cloud-gguf.md) covers a separate llama.cpp server, artifact identity, private inference networking, TLS, capability probes and measured hosting costs. Cloud resources and model weights are supplied separately.

## LLM usage and quotas

Workflow calls now have an owner-scoped invocation ledger, concurrency-safe request/token quotas, and optional versioned cost estimates. Staff manages quota policies; unknown provider usage stays unresolved until reconciliation. See [LLM usage and quotas](docs/llm-usage.md) for API access, reservation semantics and operational limits.

[Subscriptions and metered overage](docs/subscriptions-billing.md) adds immutable monthly plans, paid calendar periods, consented overage caps and internal invoices. Billing is disabled by default; payment confirmation is a staff attestation until a payment provider is selected.

## Checks

Use a PostgreSQL test database and a role allowed to create the test database:

```sh
uv sync --frozen --all-extras
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run build
DJANGO_SETTINGS_MODULE=config.settings.test uv run --frozen --all-extras pytest
uv run --frozen python manage.py makemigrations --check --dry-run
uv run --frozen ruff check .
uv run --frozen pyright
uv build --wheel
uv run --frozen python scripts/wheel_smoke.py
```

The wheel smoke check verifies both the base installation and the AWS/GCP extras in an isolated environment outside the checkout. It needs package/Git network access. CI also checks migrations and runs the suite against PostgreSQL 16 with all extras installed.

## Production

Use `DJANGO_SETTINGS_MODULE=config.settings.prod` for every process. Supply a generated `SECRET_KEY` of at least 50 characters, a PostgreSQL `DATABASE_URL`, explicit JSON `ALLOWED_HOSTS`, and the real LLM credentials through your secret manager. Production settings fail when required configuration is absent. Configure HTTPS at the reverse proxy and explicitly list allowed frontend origins if needed. Set `TRUST_PROXY_HEADERS=true` only behind a trusted proxy that overwrites forwarding headers and blocks direct client access to the app.

Before starting web and worker processes:

```sh
uv run --frozen python manage.py check --deploy
uv run --frozen python manage.py migrate --noinput
uv run --frozen python manage.py collectstatic --noinput
```

Start `uvicorn config.asgi:application` and `python manage.py runworker` as separate supervised processes using the same release and database. Configure storage retention, metrics, alerts and tested backup/restore before accepting real workloads. The Docker image supports the same commands. In a managed build environment that needs a custom CA, pass a combined trusted bundle as an optional BuildKit secret: `docker build --secret id=proxy_ca,src=/path/to/combined-ca.pem .`; the certificate is mounted only during dependency installation.

[Operations](docs/operations.md) covers migrations, leases, retries, backup and recovery. The [MIT license](LICENSE) applies to this repository; dependencies retain their own licenses.


Execution controls use the same Token header. `POST /api/v2/runs/{run_id}/resume` accepts `{"node_id":"approval","input":{"approved":true}}` and queues durable work; it does not execute the graph in the HTTP request. Select a node explicitly when multiple approvals are waiting. `POST /api/v2/runs/{run_id}/cancel` requests cancellation; a running tool stops at its timeout or the next execution checkpoint.

Staff can inspect aggregate queue/run statistics with `GET /api/v2/operations`: status counts, oldest pending age, expired processing leases and persisted token usage. This endpoint does not expose payloads or calculate monetary charges. Ready nodes currently execute sequentially in dependency order.
