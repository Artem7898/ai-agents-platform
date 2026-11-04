# Secret Manager backends and key rotation

Managed LLM runtimes can resolve credentials from Vault KV v2, AWS Secrets Manager and Google Secret Manager at the same time. Each runtime still stores only a `secret_ref` alias. The server maps that alias to one approved backend and locator; provider keys do not enter runtime records, run snapshots or API responses.

The feature applies to agents with a managed `runtime_id`. Agents with `runtime_id: null` retain the existing global `OPENAI_API_KEY` client. Assign those agents a managed runtime to use external secret resolution and rotation.

## Install and configure

Vault uses the existing HTTPX dependency. Install the optional SDK extras for the cloud backends you use:

```sh
uv sync --frozen --extra aws --extra gcp
```

The supplied Docker image installs both cloud SDK extras. Use the same extras in web and worker deployments. Development and CI checks also need `--extra dev`, or use `uv sync --frozen --all-extras`.

Configure `LLM_SECRET_BINDINGS` as server-side JSON. This example uses all three backends:

```json
{
  "vault-team": {
    "backend": "vault",
    "path": "ai-agents/team-a",
    "field": "api_key"
  },
  "aws-team": {
    "backend": "aws",
    "secret_id": "ai-agents/team-b",
    "field": "api_key"
  },
  "gcp-team": {
    "backend": "gcp",
    "project": "123456789012",
    "secret_id": "team-c-llm",
    "field": "api_key"
  }
}
```

Use a single-line JSON value when placing this setting in `.env`. These locators belong in protected deployment configuration, rather than runtime API requests. A runtime assigned `secret_ref: "aws-team"` reads only that configured binding. An explicit external binding takes precedence over an identically named entry in `LLM_RUNTIME_SECRETS`; retrieval failure never falls back to that entry or to a global environment key.

`LLM_RUNTIME_SECRETS` remains available for local development and aliases without an external binding. Its values are parsed as `SecretStr`. Environment settings load at process startup, so changing aliases, backend locators or environment-provided bootstrap credentials requires updating the affected processes. Updating an LLM credential in its configured secret store does not require restarting workers.

| Setting | Default | Behavior |
| --- | --- | --- |
| `LLM_SECRET_CACHE_TTL_SECONDS` | `30` | Positive secret-cache lifetime, from 0 to 300 seconds. Zero fetches before each client lease. |
| `LLM_SECRET_CACHE_MAX_ENTRIES` | `128` | Bounds the process-local positive cache, from 1 to 1024 entries. |
| `LLM_SECRET_TIMEOUT_SECONDS` | `5` | Retrieval wait deadline, from 0.1 to 30 seconds; also configures backend transport timeouts. |

The resolver coalesces concurrent requests for an alias and checks configuration generations before returning a credential. A late response from an obsolete configuration cannot replace a newer credential. Cancelling one caller does not cancel a shared refresh needed by another caller.

A caller's deadline does not start duplicate SDK reads while an existing physical read is still finishing. Blocking cloud SDK work cannot be interrupted safely; its client is closed after completion. Results arriving after the fetch deadline are discarded. Vault also enforces a total deadline across bootstrap-token reading and the complete response stream.

## Vault KV v2

Configure the Vault connection separately from its alias bindings:

```dotenv
LLM_VAULT_URL=https://vault.example.com
LLM_VAULT_MOUNT=secret
LLM_VAULT_NAMESPACE=
LLM_VAULT_TOKEN_FILE=/run/secrets/vault-token
```

Supply exactly one of `LLM_VAULT_TOKEN_FILE` or `LLM_VAULT_TOKEN`. A token file can be maintained by an external Vault Agent and is read again on each physical secret fetch. Protect its ownership and permissions. The application does not perform Vault login, token renewal or writes to the secret store.

Only HTTPS is accepted. TLS verification stays enabled and redirects are disabled. The mount and path use validated segments; a configured binding reads `/v1/<mount>/data/<path>`. `LLM_VAULT_NAMESPACE` is optional for deployments that use Vault namespaces. The adapter checks KV v2 version metadata and rejects deleted or destroyed versions.

For the example binding, a narrowly scoped Vault policy needs data-read access:

```hcl
path "secret/data/ai-agents/team-a" {
  capabilities = ["read"]
}
```

The KV entry must contain an `api_key` string. Keep its write/rotation permission separate from the application's read identity.

## AWS Secrets Manager

Install the `aws` extra and provide AWS credentials through the SDK's normal credential chain, such as a workload IAM role. `LLM_AWS_REGION` can select a region; otherwise the configured SDK region applies. The application does not add an API for uploading AWS access keys.

Each binding supplies an approved secret name or ARN. Reads explicitly request `AWSCURRENT`. The adapter accepts `SecretString`; `SecretBinary` is not supported. With `field`, `SecretString` must be a JSON object containing that string field. Without `field`, the entire `SecretString` is the LLM key.

Grant `secretsmanager:GetSecretValue` only on the required secret ARNs. A secret encrypted with a customer-managed KMS key can also require `kms:Decrypt` on that key and an appropriate KMS key policy. The runtime identity needs no create, update, rotate or delete permissions.

SDK retrieval retries are disabled. Blocking SDK work runs outside the event loop and uses the configured connection/read timeout.

## Google Secret Manager

Install the `gcp` extra and use the SDK's normal Application Default Credentials, preferably a workload service account. Grant `roles/secretmanager.secretAccessor` on the required secrets.

Use the numeric **project number** in `project`, or set the shared default `LLM_GCP_PROJECT` to that number. Project ID strings are not accepted by this adapter: numeric names allow exact verification of the canonical resource returned by Google.

The adapter reads `projects/<number>/secrets/<secret_id>/versions/latest`, verifies the returned resource and concrete version, and checks the payload's CRC32C checksum before using its UTF-8 value. A binding's optional `field` extracts a string from a JSON payload; without it, the entire decoded payload is the key. SDK retries are disabled and blocking SDK work runs outside the event loop.

## Rotation and revocation

Positive secret values are cached per process/event loop. On expiry, a refresh is required before acquiring an LLM client. An unavailable, denied, deleted or invalid secret blocks that new request; the application does not reuse an expired credential as a fallback. A still-valid cache entry can remain usable until its TTL expires, so backend permission revocation is not an instantaneous fleet-wide cache purge.

When a new value is observed, subsequent leases use a replacement SDK client. A request that already holds the old client retains it until release; retired clients close after their final active lease. In-flight requests cannot be recalled, and revoking their provider key early can make them fail.

Use a two-phase rotation:

1. Create the new LLM-provider key while the previous key remains valid.
2. Publish it as the latest Vault/GCP value or the AWS `AWSCURRENT` value under the existing alias binding.
3. Allow every worker's cache to refresh and wait for old in-flight requests to drain. Include retrieval time, retries and request duration in the rollout window.
4. Revoke the old key at the LLM provider and verify subsequent requests use the new version.

For an emergency stop, staff can disable the managed runtime with its existing `enabled` control. Workflow execution checks that live state before subsequent provider calls, including queued or resumed runs. This cannot undo a request already sent. A process-local cache reset does not invalidate other workers; this feature does not expose a misleading global-refresh API.

Rotation of the LLM key is separate from renewal or replacement of the bootstrap identity used to access Vault, AWS or GCP. Operate that identity through the chosen provider/agent's supported lifecycle. Changing an environment-provided bootstrap credential requires process reconfiguration; Vault token-file refresh is described above.

## Audit, testing and deployment

Secret-resolution audit records contain permitted metadata such as backend, alias and outcome. Keys, bootstrap tokens, key hashes, secret locators and raw SDK response/error bodies are excluded. Vendor HTTP/SDK debug logging is suppressed to avoid dumping credential-bearing traffic. Route these application logs to access-controlled storage with an appropriate retention policy.

Lifecycle events are `llm_secret_refreshed`, `llm_secret_rotated` and `llm_secret_refresh_failed`; successful cache hits do not generate a separate refresh event.

LLM key values must be nonempty, bounded, printable ASCII strings without control characters or surrounding whitespace, matching the provider client's HTTP authorization header requirements. Adapters bound payload size before accepting a value. Secrets are resolved at dispatch, rather than stored in the immutable run snapshot, so the same queued run can use a rotated key while retaining its frozen model and endpoint.

Run the same release, bindings, endpoint policy and installed extras in web and worker processes. No database migration is required for these adapters. Configure actual backend identities, TLS trust, outbound access and read permissions in the deployment before enabling real workloads.

The tests use mocked Vault HTTP and cloud SDK responses. They exercise rotation, expiry, cancellation, isolation and failure handling without reading or changing a real cloud secret. This feature does not provision Vault, cloud projects, IAM identities or secrets, and does not rotate the provider key on the operator's behalf.
