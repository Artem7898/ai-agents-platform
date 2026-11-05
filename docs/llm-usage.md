# LLM invocation accounting and quotas

Workflow execution records each outbound LLM attempt, including transport retries, in an invocation ledger. The ledger contains identifiers, status, token counters and optional estimated cost. It does not contain prompts, completions, tool arguments, API keys or raw provider error bodies. Standalone `LLMService` calls without a workflow invocation context keep their existing behavior and are not metered.

## Access and inspection

Use `Authorization: Token <key>`, as for the other APIs. Owners can read their invocation records, quota policies and quota windows; staff can inspect all records. Invocation ownership is frozen from the run's billing owner, so editing the workflow owner does not transfer its historical accounting records.

- `GET /api/v1/llm-calls/` lists attempts; `?run_id=RUN_UUID` filters a run.
- `GET /api/v1/llm-calls/INVOCATION_UUID/` inspects one attempt.
- `GET /api/v1/llm-quotas/` lists quota policies.
- `GET /api/v1/llm-quota-windows/` lists window counters.
- `GET /api/v1/llm-rates/` lists visible price versions: rates for owned runtimes and global legacy rates.

List endpoints use limit/offset pagination with a maximum `limit` of 200. A foreign record is not visible to an ordinary owner, including when its identifier is known.

The existing `GET /api/v2/runs/{run_id}/events` stream also carries `llm.reserved`, `llm.dispatched`, `llm.finalized`, `llm.quota_denied` and `llm.reconciled` events. They include invocation identifiers, status and token counters for progress inspection; the owner-scoped ledger API above provides the current accounting record.

`logical_call` identifies a call within a workflow node; `attempt` distinguishes actual provider attempts and retries. A quota rejection is also recorded but is not sent to the provider. An admitted attempt reserves capacity before dispatch, so concurrent workers cannot each spend the same remaining allowance. Request capacity is charged when dispatch is recorded. A retry needs its own admission and consumes another request allowance.

## Quota policies and windows

Staff creates a policy using `POST /api/v1/llm-quotas/` and changes limits or enables/disables it using `PATCH /api/v1/llm-quotas/POLICY_ID/`:

```json
{
  "owner": 42,
  "request_limit": 500,
  "token_limit": 1000000,
  "period_seconds": 86400,
  "enabled": true
}
```

Each owner has at most one policy. A null limit leaves that dimension unlimited. Without a policy, or with its policy disabled, there is no owner quota admission limit and workflow calls are still recorded; the workflow's own execution limits still apply. Ordinary owners cannot change a policy, reconcile an invocation or reset counters. The API does not delete quota policies or windows. A policy's owner and `period_seconds` cannot change after creation.

Windows are fixed intervals anchored to the policy's `created_at`, using UTC timestamps. The default period is 86,400 seconds. These are elapsed-time windows, not calendar days or monthly subscription cycles. Window boundaries are returned as `starts_at` and `ends_at`; old reservations remain associated with their original window when a later window opens.

Request admission is exact for the configured request counter. Token admission uses a conservative input estimate plus the requested output bound. This reservation protects concurrent admission but is **not a guaranteed exact hard cap on the provider's billed tokens or money**: provider tokenization and usage semantics can differ. Reported usage above a reservation is recorded in full and can leave a window over its limit; later admissions are then blocked. Lowering a policy's limits does not erase counters or outstanding reservations.

A run's `max_tokens` also uses the authoritative invocation ledger: reported actual usage plus unresolved token holds, across retries and worker restarts. Admission can reject a call before contacting the provider when the conservative reservation is larger than the run's remaining budget. Checkpoint `usage.total_tokens` contains reported totals; `usage.budget_tokens` also reflects unresolved holds and can therefore be larger. Checkpoints are a progress view; the ledger controls admission and survives checkpoint recovery.

## Unknown usage and reconciliation

Token counters are null when usage is missing. Null means unknown, not zero. A provider failure, timeout, interrupted stream or worker crash may still have reached the provider and incurred cost. Its unresolved token reservation remains held until an operator resolves it; a missing usage response does not silently free that allowance. A definite cancellation before dispatch can release its reservation.

Staff can reconcile an unknown or stale invocation after checking the provider's records:

```sh
curl --fail-with-body http://127.0.0.1:8000/api/v1/llm-calls/INVOCATION_UUID/reconcile/ \
  -H 'Authorization: Token STAFF_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"prompt_tokens":120,"completion_tokens":30,"total_tokens":150}'
```

When the operator has established that the request was never sent, use `{"not_sent":true}` instead of token counts. Reconciliation refuses a still-active execution lease and does not overwrite known provider usage. Repeating the same completed reconciliation is idempotent; a conflicting result is rejected. There is no blanket reset endpoint. Keep an external audit record of the evidence used for an operator reconciliation.

## Optional price estimates

Staff can create an immutable rate version with `POST /api/v1/llm-rates/`. A rate applies to a runtime and model; a null runtime is the global legacy configuration. Prices are Decimal values per million input and output tokens with an effective timestamp and currency. A later price requires a new version rather than editing history. For example:

```json
{
  "runtime": "RUNTIME_UUID",
  "model_name": "gpt-4o-mini",
  "version": "2026-11-01",
  "effective_from": "2026-11-01T00:00:00Z",
  "currency": "USD",
  "input_per_million": "0.15",
  "output_per_million": "0.60"
}
```

These sample prices only illustrate the format; configure the provider's actual applicable tariff.

Admission selects the applicable rate version and freezes it for that invocation. With no applicable rate, estimated cost is null; the model is not assumed to be free. Missing input or output counts leave cost unknown. A total-only token count cannot establish an input/output price split. These values are usage-based estimates, not a wallet balance, invoice or payment charge. Provider-specific cached-token, reasoning-token, batch and other pricing rules need separate billing support.

## Scope and operations

Apply Django migrations before starting web and worker processes. Use the same database and release for all workers; PostgreSQL row locks serialize quota admissions. Token and request policies are independent of a workflow's existing execution limits.

Accounting, admission quotas and optional price estimates also cover [tool-call repairs](schema-first-tools.md) and [structured workflow results](structured-results.md). [Secret Manager adapters](secret-manager.md) support external key resolution; the [GGUF pilot](cloud-gguf.md) prepares a separately hosted inference server. Hosted GPU provisioning, subscription payments and provider-billing synchronization remain separate work. Configure retention and export of invocation records separately before relying on them for long-term financial reporting.
