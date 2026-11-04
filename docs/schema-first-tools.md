# Schema-first tools and bounded repairs

Tool registration builds a strict Pydantic argument model from the function's annotated parameters. The same model defines the advertised tool schema and validates arguments before Python execution. Unknown fields, invalid types and non-finite numbers are rejected. JSON forms such as UUID and date strings are validated in JSON mode; validated nested Pydantic values retain their Python types when passed to the function.

## Validation before execution

An LLM node validates every call in a returned batch before dispatching its first tool. If any call has invalid JSON or fails its argument schema, the entire batch is rejected without executing any tool from that batch. A valid call elsewhere in a rejected batch is not executed.

Only argument validation failures can request a model correction. Unknown or forbidden tools, invalid call metadata, duplicate IDs and IDs reused from accepted rounds fail execution. The agent's `tool_ids` remains an enforced allowlist at validation and execution boundaries.

Errors raised by a tool after dispatch do not enter the repair loop. Retrying such an operation could repeat an external effect. Tool backends still need to honor the stable idempotency key supplied for recovery; this does not guarantee exactly-once effects in another system. A recovered batch is validated again, and a partially executed batch cannot be replaced through model repair.

## Configure repairs

Set `max_tool_repairs` in the workflow's existing `limits` object:

```json
{
  "limits": {
    "max_tool_repairs": 2,
    "max_tool_calls": 8,
    "max_tokens": 32000,
    "max_duration_seconds": 300
  }
}
```

The default allows two corrections per LLM node; accepted values are zero through five. Zero preserves fail-fast behavior. The repair allowance covers the node as a whole and does not reset after a successful round. This limit is separate from the tool execution budget.

Before the next LLM request, the executor persists the repair count, round count and safe correction instruction in its checkpoint. Rejected assistant calls are discarded instead of creating unresolved tool-call messages in the conversation. Worker recovery retains the correction allowance recorded in the checkpoint.

`max_tool_repairs` counts granted semantic corrections per node, not physical HTTP attempts. Transport retries can send more than one request. A worker crash after a provider response but before checkpointing can also repeat the same granted round; each physical attempt is still recorded and subject to the run budget and owner quota. The platform cannot guarantee exactly-once requests to an arbitrary model provider.

Corrections use the normal provider transport and therefore consume request quota, token budget and elapsed time. Every provider attempt is recorded in the [invocation ledger](llm-usage.md), including a response whose arguments fail validation. Missing usage retains its reservation. Cancellation, provider failures and exhausted quotas keep their existing handling; they do not create extra validation retries. Conversation message and request-size limits also continue to apply.

Validation feedback contains bounded error codes and schema paths, without the rejected argument values or raw Pydantic error text. Treat normal workflow checkpoints and events as application data: accepted prompts, calls and tool results can still contain sensitive information.

The existing `/api/v2/runs/{run_id}/events` endpoint includes `tool_validation_failed` events with `node_id`, `repair_count`, `will_retry` and safe `issues` entries containing `code` and `path`. A final validation failure records `will_retry: false` before the node fails.

## Typed execution context

A registered tool can declare a `ToolExecutionContext` parameter. The executor supplies this immutable value from the run; the parameter is excluded from the model-facing argument schema.

```python
from src.agents.tool_context import ToolExecutionContext
from src.agents.tool_registry import ToolRegistry


@ToolRegistry.register(description="Return a run-scoped reference for a document.")
async def document_reference(
    document_id: str,
    context: ToolExecutionContext,
) -> dict[str, str]:
    return {
        "document_id": document_id,
        "run_id": str(context.run_id),
        "node_id": context.node_id,
    }
```

The context carries `run_id`, `workflow_id`, `node_id`, `tool_call_id`, the frozen billing `owner_id`, and `idempotency_key`. It contains identifiers rather than ORM objects, provider credentials or an unrestricted backend dependency container. A model cannot supply or replace it through its arguments. Direct `ToolRegistry.execute` callers must supply `execution_context` when a tool requires this dependency.

Context injection does not sandbox arbitrary Python code. Registered tools remain trusted server code and must enforce any resource-specific authorization when they access a database or external system.

## Structured outputs and compatibility

`LLMService.generate_structured` already sends a native `response_format` JSON schema and validates the result with the target Pydantic model. Provider capability declarations remain explicit: an endpoint that does not support structured outputs or tools is rejected before a request. Local validation remains necessary even when a provider advertises strict schemas.

The tool repair loop uses `max_tool_repairs`. [Structured workflow results](structured-results.md) have a separate output contract and `max_output_repairs` policy. Standalone structured-output calls do not gain an automatic semantic correction loop. Local validation remains necessary, and a correction cannot guarantee a valid model result. The implementation uses the project's existing Pydantic and OpenAI SDK dependencies; PydanticAI is not required.

`max_tool_repairs` is stored in the workflow's JSON limits and frozen run snapshot. This stage requires no new database migration. New runs use the default of two corrections, including runs created from older workflow definitions that omit the field. Existing frozen run snapshots without the field retain fail-fast behavior with zero corrections. Use the same release across web and worker processes so both accept the same workflow schema.
