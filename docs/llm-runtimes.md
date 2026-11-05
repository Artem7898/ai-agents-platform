# LLM runtimes

An agent can select an OpenAI, Ollama or OpenAI-compatible runtime while using
the same `AsyncOpenAI` transport. A runtime records its endpoint, permitted
models and supported request features. Credentials resolve from protected
settings or an approved external Secret Manager; the database holds only a
credential alias.

This adds managed runtime selection and compatibility profiles. Existing
agents with `runtime_id: null` continue to use the global `OPENAI_API_KEY` and
optional `OPENAI_BASE_URL`. Migrations do not move these agents to a new
provider. Runtime restrictions described below apply to managed runtimes;
configure the legacy global endpoint separately.

## Operator configuration

Run migrations before starting web and worker processes. Both processes must
use the same release, runtime endpoint policy and credentials.

The default managed-runtime allowlist contains only OpenAI. Add an endpoint
explicitly before creating a runtime that uses it:

```dotenv
LLM_ALLOWED_BASE_URLS=["https://api.openai.com/v1","http://ollama:11434/v1","https://llm.example.com/v1"]
LLM_RUNTIME_SECRETS={"cloud-main":"REPLACE_WITH_PROVIDER_KEY"}
```

These settings are JSON. The key in `LLM_RUNTIME_SECRETS` is an alias; its value
is the actual provider credential. Use a protected local `.env` during
development, or inject the JSON mapping through your deployment's secret
storage. Do not commit the populated mapping, copy it into an API request, or
store it in a runtime record. Values are parsed as `SecretStr` in settings.

`LLM_ALLOWED_BASE_URLS` is an exact endpoint allowlist using canonical URLs,
rather than a list of arbitrary domains a user can supply. The server checks
the endpoint when validating a runtime and before dispatching a request;
redirects are disabled.
Cloud profiles require HTTPS. Plain HTTP is permitted only for an explicitly
allowlisted Ollama endpoint. URLs cannot contain credentials, query strings
or fragments.

Only allow endpoints operated by you or a provider you trust to receive
workflow prompts and credentials. Listing an endpoint grants outbound access
to it; it does not verify its model behavior.

The `openai` and `openai_compatible` profiles require `secret_ref`, which must
resolve through a server-configured alias. An alias in `LLM_SECRET_BINDINGS`
selects Vault KV v2, AWS Secrets Manager or Google Secret Manager; aliases
without an external binding use `LLM_RUNTIME_SECRETS`. An explicit external
binding never falls back to that settings mapping or the global API key.
An Ollama runtime can omit `secret_ref`; the transport uses an internal SDK
placeholder for an endpoint without authentication. Set an alias if the
Ollama endpoint requires a key.

Settings are loaded at process startup. Changing `.env`, alias bindings or
environment-provided bootstrap credentials requires process reconfiguration.
For external bindings, the resolver refreshes the stored LLM key after a
bounded TTL and creates a replacement client when it observes a new version.
An active request keeps its leased client until release. A failed refresh
after expiry blocks new requests rather than reusing an expired key. See
[Secret Manager and rotation](secret-manager.md) for all three backends,
optional SDK extras, access policies and the two-phase revocation procedure.

### Ollama networking

Use an Ollama server with the OpenAI-compatible `/v1` API enabled and the model
already available there. This repository does not start an Ollama service.
`http://localhost:11434/v1` works only when Ollama is reachable on the same
network namespace as the calling worker. Inside a container, `localhost`
refers to that container. For an Ollama service on the same Compose network,
use its service name, such as `http://ollama:11434/v1`, and add that exact URL to
the allowlist. Reaching an Ollama server on the host requires a separately
configured host route and Ollama listener.

## Runtime API and access

Use the existing `Authorization: Token <key>` authentication.
`/api/v1/runtimes/` supports runtime administration by staff. Ordinary users
can list and retrieve only their own runtimes. Staff can manage owners and
shared or legacy records. A user cannot gain access to another owner's
runtime by placing its UUID on an agent.

Runtime profiles are `openai`, `ollama` and `openai_compatible`. The latter
means the endpoint accepts the configured OpenAI request features; it is not
a claim that every OpenAI feature works with every provider or model.

Runtime configuration includes:

| Field | Purpose |
| --- | --- |
| `owner` | User ID permitted to assign this runtime to their agents; defaults to the staff creator. |
| `name` | Operator-facing runtime name. |
| `profile` | Provider compatibility profile. |
| `base_url` | API endpoint from the server allowlist, including `/v1` where required. |
| `secret_ref` | Write-only alias from the server settings or external-secret bindings. |
| `allowed_models` | Model names this runtime permits agents to request. |
| `capabilities` | Explicit declarations of supported request features. |
| `enabled` | Whether the runtime may serve new provider calls. |
| `revision` | Server-managed configuration revision. |

For example, after configuring the Ollama service and allowlist above, a
staff user can create a runtime for user `42`. Replace that ID with an
existing target owner and select a model installed on that endpoint:

```http
POST /api/v1/runtimes/
Authorization: Token STAFF_TOKEN
Content-Type: application/json

{
  "owner": 42,
  "name": "Local Qwen",
  "profile": "ollama",
  "base_url": "http://ollama:11434/v1",
  "secret_ref": null,
  "allowed_models": ["qwen3:8b"],
  "capabilities": {
    "tools": true,
    "structured_outputs": false,
    "streaming": false,
    "usage": true,
    "strict_tools": false,
    "token_limit_parameter": "max_tokens",
    "temperature": true
  },
  "enabled": true
}
```

This is an example profile declaration, not a certification of that model's
tool support. For a cloud runtime, use its HTTPS endpoint and set
`"secret_ref": "cloud-main"`; the API response omits this write-only field.
Aliases accept letters, digits, `_`, `.` and `-`, start with a letter or digit,
and are limited to 128 characters. `allowed_models` must be a nonempty list
without duplicates. `revision` starts at 1 and increments when persisted
configuration changes; clients do not set it themselves.

Assign the runtime through the agent API:

```http
PATCH /api/v1/agents/AGENT_UUID/
Authorization: Token YOUR_TOKEN
Content-Type: application/json

{"runtime_id":"RUNTIME_UUID"}
```

Set the agent's `model_name` to a model present in that runtime's
`allowed_models`. Use the exact model name exposed by the endpoint; for
Ollama this includes the model tag when applicable.

### Capabilities

Boolean capabilities default to `false`. Enable only the features verified
for the selected endpoint and model. Token-limit parameter defaults depend
on the profile: `max_completion_tokens` for `openai`, `max_tokens` for
`ollama` and `openai_compatible`.

| Capability | Behavior |
| --- | --- |
| `tools` | Allows requests with tool definitions. |
| `strict_tools` | Sends strict tool schemas when enabled; otherwise sends ordinary tool schemas. |
| `structured_outputs` | Allows JSON Schema structured-output requests. |
| `streaming` | Allows streaming calls. |
| `usage` | Declares provider support for token-usage reporting. |
| `temperature` | Includes the agent temperature when enabled; omits it otherwise. |
| `token_limit_parameter` | Selects `max_tokens` or `max_completion_tokens` on the wire. |

`strict_tools` requires `tools` to be enabled.

Returned token usage is parsed when present. Missing counters remain null,
separate from measured zero. Workflow requests enter the
[invocation ledger](llm-usage.md), including retries and rejected responses;
unknown usage retains its token reservation. The `usage` capability declares
provider reporting support and does not itself configure a quota or payment
subscription.

A request that requires tools, structured output or streaming fails before
the HTTP request if its capability is disabled. The service does not silently
switch providers or downgrade a structured-output requirement to unvalidated
text. Capability declarations do not replace output validation or the
agent's `tool_ids` allowlist.

## Run snapshots and credential changes

A run freezes the selected runtime's configuration with its agent and
workflow definitions. Editing the runtime endpoint, profile, models or
capabilities therefore does not retarget an already queued or resumed run.
Snapshots contain configuration and a secret alias, not provider keys.

The live runtime's existence, enabled state and ownership remain execution
checks. Disabling a runtime or revoking access blocks subsequent provider
calls from a queued or resumed run; a request already sent to the provider
cannot be recalled. Endpoint allowlist checks also remain live.

Credentials are resolved at dispatch rather than frozen in a run. Rotation
under an external alias applies to subsequent leases after its cache refresh,
including queued or resumed runs. Rotation does not change the run's model or
endpoint. Backend access revocation is bounded by the positive-cache TTL;
use the live runtime disable control for an emergency stop of new workflow
calls. There is no automatic failover to another secret backend or LLM
provider when access, a key, model or endpoint fails.

## Compatibility checks

The pytest suite uses mocked transports and provider responses. It verifies
runtime selection, configuration and credential isolation, request shape,
capability enforcement and relevant failure paths without spending provider
credits. It does not certify a real provider, subscription or custom model.

Before production use, run a small workflow against the exact provider/model
combination from the worker's deployment network. Check plain completion,
any required tools and structured output, output-token limits, reported
usage, timeouts and provider errors. Enable streaming only after checking
its response shape and usage behavior. Keep the test workload bounded and
record the model/version tested; compatibility can change with the provider.

## Follow-up backlog

The runtime registry and profile tests establish the basis for switching
between approved endpoints. The remaining work needs separate acceptance
criteria:

1. **Deployment-specific secret operations.**
   [Vault, AWS and GCP adapters](secret-manager.md), bounded cache refresh and
   lease-safe key rotation are implemented. Provision actual backend
   identities, scoped read policies, secrets, network access and log retention
   in the target environment, and exercise its rotation/revocation procedure.
2. **Subscription billing and provider reconciliation, if required.**
   [Call accounting and quotas](llm-usage.md),
   [bounded tool repair](schema-first-tools.md) and
   [structured workflow results](structured-results.md) are implemented.
   Payment collection, subscription entitlements, calendar billing cycles
   and automated reconciliation with actual provider charges remain separate
   product work; usage-based cost estimates do not implement those features.
3. **A measured GGUF deployment.** The [portable GGUF pilot](cloud-gguf.md)
   provides a separate llama.cpp deployment configuration and bounded
   verification/probe tooling. Choose a licensed model, hosting plan and
   hardware, then measure memory, context, cold start, throughput, quality
   and total cost with that exact artifact and chat template. Model upload,
   GPU provisioning, autoscaling and cloud deployment remain
   environment-specific work; selecting a runtime URL does not perform them.

Do not infer that moving a workflow to a cloud endpoint makes it cheaper.
Compare measured quality, latency and cost, including idle capacity, request
retries and data transfer, before choosing local or hosted execution.
