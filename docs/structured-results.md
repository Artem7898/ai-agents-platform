# Structured workflow results

An `llm_call` node can select a server-registered Pydantic output contract. Its result is then a validated JSON object available to conditions, templates, checkpoints and the workflow output. Nodes without `output_schema` keep their existing text/tool-call behavior.

## Select a contract

Use `GET /api/v1/output-schemas/` with `Authorization: Token <key>` to discover the available contracts. This read-only catalog returns `id`, `version`, `description`, `json_schema` and `fingerprint`. It uses limit/offset pagination with a default limit of 50 and a maximum of 200. Workflow requests select a registered identifier and version; the API does not accept Python import paths or upload new schemas.

The built-in `review_decision` version `1` requires an `approved` boolean and a numeric `score` between zero and one:

```json
{"approved": true, "score": 0.87}
```

Set `output_schema` on the node itself, alongside `kind` and `agent_id`. It is valid only on `llm_call` nodes. A managed runtime used by this node must have `structured_outputs` enabled for a provider/model combination that supports native JSON Schema output.

For example, create this workflow using an active agent owned by the same user. Replace `AGENT_UUID` with its UUID:

```json
{
  "name": "Document review",
  "nodes": [
    {
      "id": "review",
      "kind": "llm_call",
      "agent_id": "AGENT_UUID",
      "output_schema": {"id": "review_decision", "version": 1},
      "config": {"prompt_template": "Review this document: {input.document}"}
    },
    {
      "id": "check",
      "kind": "condition",
      "config": {
        "condition": {
          "path": "nodes.review.result.approved",
          "op": "eq",
          "value": true
        }
      }
    },
    {"id": "approval", "kind": "hitl_approval"}
  ],
  "edges": [
    {"source": "review", "target": "check"},
    {"source": "check", "target": "approval", "condition": true}
  ],
  "limits": {"max_output_repairs": 2, "max_tokens": 32000},
  "output_node_id": "review"
}
```

Execute it with `input_data` containing `document`. If `approved` is true, the workflow waits at `approval`; otherwise that branch is skipped. The existing resume command continues the waiting run after a human decision.

The object is stored at `nodes.review.result` and `llm_output_review` in the workflow context. `output_node_id: "review"` selects it as `output_data.result`. Templates can reference fields such as `{nodes.review.result.score}`. Recovery preserves a saved completed result as an object and does not request that result from the provider again.

Structured mode requests a schema-bound result without advertising or executing tools. Tool-enabled reasoning can be placed in a separate preceding node when needed. The selected output contract does not change an agent's normal tool allowlist.

## Validation, corrections and accounting

The service sends a native JSON Schema response format and validates the response using the selected Pydantic model in strict JSON mode. Invalid field types, unknown fields and non-finite numbers are rejected. Accepted results must remain JSON objects with finite, stable serialization, within the 256,000-byte result limit.

`max_output_repairs` is a strict integer from zero through five and defaults to two for new runs. It grants semantic corrections per structured node and is separate from `max_tool_repairs`. Zero makes the first validation failure terminal. The executor persists the correction count, round count and safe instruction before requesting a correction. Rejected output values and raw Pydantic error messages are not copied into correction feedback or validation events.

Every provider attempt passes through the existing [invocation ledger and quotas](llm-usage.md), including responses rejected by the output schema. Corrections consume request quota, token budget and elapsed time. Missing usage retains its reservation. Transport retries and recovery after a crash before checkpointing can repeat a granted round; `max_output_repairs` does not guarantee an exact physical HTTP request count.

Refusal, truncation, unsupported runtime capabilities, exhausted quotas and transport failures follow their existing failure behavior. Unexpected tool calls and a server schema's unstable serialization contract are terminal errors rather than output corrections. No unvalidated text result is substituted for a failed structured response.

The existing run events endpoint includes `output_validation_failed` with `node_id`, `repair_count`, `will_retry` and bounded `issues` entries containing safe `code` and `path` values. Normal accepted results remain application data and can contain sensitive information; apply the same access and retention controls as for other workflow results.

## Frozen versions and server extensions

Run creation freezes each selected contract in `output_schema_snapshots`, including its identifier, version, JSON schema and fingerprint. Editing a workflow later does not retarget its existing runs. Before dispatch, the executor verifies that the frozen contract matches an available registered version. A missing version or changed JSON schema blocks the request.

Register additional contracts in trusted server code:

```python
from pydantic import BaseModel, ConfigDict, Field

from src.workflows.output_schemas import OutputSchemaRegistry


@OutputSchemaRegistry.register("document_summary", 1, description="A brief document summary.")
class DocumentSummaryV1(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", allow_inf_nan=False, validate_default=True
    )

    summary: str = Field(min_length=1, max_length=2000)
```

Import the registration module during application startup, for example from its `AppConfig.ready`, in both web and worker processes. Built-in contracts register when the workflow schema module is imported. A version is immutable: publish a new version for changes and retain old registered versions while their runs may resume. Custom validation logic changes also require a new version even when the exported JSON schema is unchanged; a schema fingerprint cannot detect changes hidden in Python validators.

Custom validators must be pure and idempotent, and serializers must preserve a stable schema-valid object. Validation/serialization is checked again before accepting or restoring a result. Arbitrary Python execution and schema registration remain trusted deployment operations.

## Deployment and compatibility

Apply Django migrations before starting web and worker processes; this release adds `runs.0007_workflowrun_output_schema_snapshots`. All processes must use the same release and registered contract versions.

Existing runs without an output contract retain their prior text/tool behavior. Frozen snapshots missing `max_output_repairs` use zero corrections. The new policy applies automatically when a new run snapshots a workflow that omits the limit.

Standalone `LLMService.generate_structured` calls still return a validated Pydantic model without an automatic semantic correction loop. Workflow nodes use the accounting-aware structured response path. The feature uses the existing OpenAI SDK and Pydantic dependencies and does not add PydanticAI, Secret Manager integration or GGUF hosting.
