# Ticket triage demo and crash proof

This local demonstration uses eight fictional Spanish tickets, a managed Ollama runtime, a schema-bound recommendation and an explicit operator decision. The only categories are **Facturación**, **Acceso**, **Soporte técnico** and **Otros**. The model returns exactly `category` and `reason`; the operator may correct the category before saving.

The workflow is `classify → approval → approved → save_ticket`. The condition compares `nodes.approval.result.approved` with literal `true`. A false decision skips the save node. The model has no tools. The save node forwards a trusted, stable key `<run_id>:save_ticket:tool` to the local HTTP destination, which commits a uniquely keyed receipt independently of the workflow checkpoint.

## Local setup

Use an existing active operator account and a PostgreSQL database. The commands do not create users, reset databases, remove volumes or stop unrelated processes. For optional dedicated infrastructure:

```bash
timeout 180 docker compose -f deploy/triage/compose.yaml up -d --wait
timeout 900 docker compose -f deploy/triage/compose.yaml exec -T ollama ollama pull qwen2.5:3b
```

Configure the application with the values in [deploy/triage/.env.example](../deploy/triage/.env.example). These are application environment variables: the optional Compose file starts PostgreSQL and Ollama only. Select an existing database/operator when those already exist. Set the exact Ollama endpoint in `LLM_ALLOWED_BASE_URLS`; model tags must match exactly. Supply a fresh `DEMO_TRIAGE_SINK_TOKEN` through the environment or an ignored `.env`. Keep billing disabled for this local demonstration.

`qwen2.5:3b` is an example candidate, not a validated model result from this workspace. The model registry was unavailable here and Ollama had no installed weights. A real seed and proof must pass against weights available in your environment before claiming model support or quality.

```bash
timeout 120 .venv/bin/python manage.py migrate --noinput
timeout 360 .venv/bin/python manage.py triage_demo seed \
  --operator existing-username \
  --base-url http://127.0.0.1:11435/v1 --model qwen2.5:3b \
  --output deploy/triage/reports/seed
```

Seeding creates a **disabled** runtime with a single allowed model. It calls Ollama `/api/version`, `/api/tags` and the OpenAI-compatible structured completion endpoint with the actual ticket schema. It strictly validates the response before enabling `structured_outputs`. Other capabilities remain disabled. A failed probe leaves that new runtime disabled and writes `probe-failure.json`; it never silently substitutes a mock or pulls weights. `scenario.json` records the model digest, version, prompt, exact request, response and runtime identity. Every seed creates a fresh workflow version and agent, preserving existing run definitions.

## Operator review

```bash
timeout 360 .venv/bin/python manage.py triage_demo start \
  --operator existing-username \
  --scenario deploy/triage/reports/seed/scenario.json \
  --ticket ES-001 --output deploy/triage/reports/review
```

This command starts a worker restricted to the new run and waits for `waiting_human`. It prints the exact run ID and `/console/triage/`. Start the application web server on port 8010, matching the configured `DEMO_TRIAGE_SINK_URL`:

```bash
timeout 1800 .venv/bin/python manage.py runserver 127.0.0.1:8010 --noreload
```

Open `http://127.0.0.1:8010/console/triage/` as that operator, inspect the recommendation, choose the final category and explicitly confirm. The console queues the decision. In a separate terminal, process that exact run ID:

```bash
timeout 120 .venv/bin/python manage.py triage_demo_worker --run-id RUN_UUID
```

The save adapter uses the configured local HTTP sink. `--queue-only` creates the run without starting a worker. The CLI never automatically approves a `start` run.

## Repeatable proof

```bash
timeout 1800 .venv/bin/python manage.py triage_demo proof \
  --operator existing-username \
  --scenario deploy/triage/reports/seed/scenario.json \
  --output deploy/triage/reports/proof
```

Use `OUTBOX_LEASE_SECONDS=15` and a database without an unscoped worker competing for demo commands; the optional dedicated database above is recommended. The proof requires PostgreSQL, re-probes the current model and rejects a changed digest. It starts its own temporary local sink and workers, each restricted to a known run ID. It never signals a shared worker. The proof evaluates all eight Spanish tickets and three equivalent Spanish/English/Russian smoke inputs through the real workflow. Expected labels live in a separate file and are never included in model inputs. It records a scripted correction and a scripted false approval with zero destination rows. **These decisions are scripted operator actions, not evidence that a person reviewed the tickets.** Real-model acceptance requires the central invoice to be classified as Facturación and all three access smoke tickets as Acceso. Other fixture accuracy is measured and reported. Mocked evidence explicitly leaves model quality unassessed. Provider digest and version are checked again after inference. Failed quality or identity checks export their evidence before returning an error.

For the crash run, the destination commits its receipt and deliberately holds the HTTP response for up to 60 seconds. The supervisor observes that committed row while the save checkpoint is unfinished, sends `SIGKILL` only to its own worker, reads the actual persisted lease expiry, waits for it to pass and starts a replacement worker for the **same run**. No database statuses, tokens or lease timestamps are adjusted. The original resume command is reclaimed for attempt two; the sink receives the same key, returns the original receipt and retains exactly one row. The proof fails if any of these checks fail.

Every provider/worker action has a bounded timeout (`--timeout`, default 300 seconds, allowed 10–600); startup and lease waits have separate short bounds. The overall shell timeout above also bounds the full scenario. Only owned children are terminated during cleanup. Use a fresh output directory for each execution; artifact directories under `deploy/triage/reports/` are ignored by Git.

## Evidence

`manifest.json` records the exact baseline Git SHA, dirty status, tracked binary patch, untracked source snapshots and SHA-256 of every artifact. A starting source fingerprint is compared with the final tracked patch and untracked source hashes; a source change during execution makes the proof fail. Only recognized source paths are captured; untracked private or credential-like files are refused and must be ignored before exporting. It identifies real versus mocked provider evidence, Ollama version and model digest, operator identity, shared PostgreSQL and lease settings. Configuration exports are explicitly selected and contain no sink token, credentials or environment dump. Child processes are stopped before logs are hashed.

The bundle contains input fixtures, separate expected labels, prompt/schema, runtime and definition snapshots, every run checkpoint and outbox command, worker/sink logs, paginated event API responses (`limit=5`, checked for omissions/duplicates), destination attempt audit, original/replayed receipts and the SIGKILL/expiry timeline. Event pages use the actual authenticated DRF API in process and are labeled accordingly. `crash/committed-before-kill.json` and `crash/recovery.json` demonstrate the unfinished checkpoint and successful replay. Dirty source evidence must be applied to the baseline SHA; it must not be described as a clean committed revision.

To export selected existing owner runs without invoking the model:

```bash
timeout 120 .venv/bin/python manage.py triage_demo evidence \
  --operator existing-username --run-id RUN_UUID \
  --output deploy/triage/reports/manual-review
```

The integration test below uses a **mock HTTP provider**, real PostgreSQL, real subprocess workers, real sink HTTP requests, an actual `SIGKILL` and the natural 15-second lease expiry. It validates durability and protocol behavior; it does not validate Ollama weights, multilingual understanding or model accuracy. Its seed/proof commands pass `--mock-provider`, and all resulting manifests identify `mock_ollama_protocol`.

```bash
timeout 240 .venv/bin/python -m pytest -q -s tests/integration/test_triage_harness.py
```

When models or the model registry are unavailable, record that blocker and retain the disabled-runtime probe failure. Do not describe protocol-fixture results as real-model results.

CI runs the mocked-provider integration proof and uploads its `demo-evidence/` artifacts with the test reports, including failure artifacts. Those bundles verify workflow durability and protocol handling. Real model inference and model quality remain unverified by CI; they require a successful real-provider proof without `--mock-provider`.
