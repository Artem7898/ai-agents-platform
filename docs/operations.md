# Operations and recovery

## Release and migration

Use the same pinned release, `uv.lock`, settings and database for web and worker. Run `uv sync --frozen --no-dev`; never update dependencies implicitly during a release. Production uses `config.settings.prod`, explicit hostnames, a generated secret key and PostgreSQL. Keep API tokens, LLM keys and backups in access-controlled storage.

1. Verify CI, wheel installation and `python manage.py check --deploy` with the intended settings.
2. Take a database backup and verify that a recent restore test succeeded.
3. Stop admission of new work and drain workers where practical. A killed process may have already performed an external side effect before saving its checkpoint.
4. Run migrations once: `python manage.py migrate --noinput`. Run `collectstatic --noinput` if serving the admin.
5. Start web and worker processes from the same release. Verify readiness and execute a harmless test workflow.
6. Resume admission; watch pending message age, expired leases, failure rate and LLM cost.

For an existing installation with domain tables created outside migrations, inspect the actual schema before applying initial migrations. Test the upgrade on a restored copy. Use `--fake-initial` only after confirming that existing tables match the initial migration; it does not reconcile incompatible columns. Do not reset a live database to get past migration errors.

## Queue and worker

Start the consumer with `python manage.py runworker`. For a bounded operational check use `python manage.py runworker --once`; `--batch-size` and `--poll-interval` tune polling. All worker processes must share the database and configuration.

`OUTBOX_LEASE_SECONDS` defaults to 600 and `OUTBOX_MAX_ATTEMPTS` to 3. A claimed message has a lease; expired claims can be retried with backoff. Alert before the oldest pending message exceeds the expected workload duration and when failed messages or expired leases accumulate. Tune leases with the maximum run duration and operational recovery time in mind.

Delivery is at least once. The database cannot atomically commit a payment, message or other effect in a remote API together with a local checkpoint. Tools with side effects must use a stable idempotency key at the destination or be reconciled before retry. Do not manually change `processing` to `pending` to hide an unknown result. Inspect the run, its attempt, checkpoint and remote action first.

Human input must be submitted through the authenticated resume endpoint. Do not edit checkpoint JSON or run status directly to continue a run. A repeated command must not be used as a substitute for investigating a failed external effect.

## Health and observability

Use liveness to determine whether the process responds and readiness to determine whether it can serve work. A healthy web response does not establish that a worker is consuming messages.

Collect and correlate `run_id`, node and attempt information from logs and events. Track queue age, pending/processing/failed counts, expired leases, node failures, run duration, LLM tokens/cost and cancellation. Protect event payloads and tool results as application data; restrict access and define retention before storing personal or customer information.

The tracing implementation supports application spans, but exporters, a metrics backend, alert routing and deployment-specific SLOs must be configured and tested by the operator. Measure latency with realistic workflows and concurrency; do not infer p95 from a client timeout.

## PostgreSQL backup and restore

Use your managed PostgreSQL provider's backup and point-in-time recovery where available. A simple logical backup for a small installation is:

```sh
pg_dump --format=custom --file=agents.dump "$DATABASE_URL"
```

Avoid exposing database URLs in shared terminals or logs; a PostgreSQL service file or protected credentials file is preferable. Encrypt the backup and keep it away from the application container.

Restore into a separate database, with web and workers pointed away from it:

```sh
pg_restore --no-owner --dbname="$RESTORE_DATABASE_URL" agents.dump
```

Verify schema migrations, ownership/access controls, recent runs, checkpoints, event order and queue state. Restoring a database can replay old outbox commands: reconcile external side effects before enabling its workers. Validate the restore process regularly and record recovery time and acceptable data loss. Never test recovery by overwriting the only live database.

## Rollback and incidents

Prefer backward-compatible schema changes and deploy fixes forward. Rolling application code back does not reverse data migrations or external tool effects. Before using an older release, test its compatibility with the current schema and checkpoint version on a copy.

If work stops, check database readiness and worker logs, then pending age, leases, retry exhaustion and the run's persisted state. Keep failed records for diagnosis. Stop workers when external action outcomes are ambiguous; reconcile outcomes and restore service with a reviewed procedure. Revoke compromised API tokens and rotate affected credentials through the secret manager.


## Tool policy and event history

Definition owners are trusted workflow builders and can select from the server's
registered tool catalogue. Agent `tool_ids` restrict LLM dispatch; an explicit
tool node grants only its selected tool. Registering a tool exposes it to these
builders, so domain-specific backend authorization belongs inside sensitive tool
implementations. The current catalogue contains a read-only search adapter whose
endpoint is configured by the operator.

Checkpoints are the durable execution state. Status transitions and their events
commit atomically. Node timeline events are appended separately, so a process
crash can leave an incomplete node timeline even when recovery correctly skips
a completed node. The event API is an inspection log, not arbitrary event replay
or rollback of external effects.


## Aggregate operational statistics

`GET /api/v2/operations` requires an active staff Token. It reports run and command counts, pending queue age, expired processing leases and persisted token totals. It does not report an idle worker heartbeat or monetary billing. Poll this protected endpoint into your monitoring system and configure alerts for your workload.

## Existing installations

Back up first and test migrations against a restored copy. Legacy definitions without owners remain visible only to staff. The data migration freezes existing run definitions and imports legacy events without changing their original input data. Invalid legacy graphs still require correction before execution.

If an earlier installation created the agents table manually with `--run-syncdb`, inspect its schema before applying the new initial migration. Django's `--fake-initial` is appropriate only after verifying that the existing table matches the initial migration; a fresh database uses ordinary `migrate`. No live database is migrated by opening this PR.
