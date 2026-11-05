# Portable GGUF pilot

This pilot prepares a single-model llama.cpp server for the platform's existing OpenAI-compatible runtime. It provides a CPU configuration, an optional NVIDIA GPU override, a TLS proxy, artifact verification and bounded protocol/benchmark commands. Cloud resources, model weights and their operating budget are supplied by the operator; creating this configuration does not start a cloud service.

The application continues to use `profile: "openai_compatible"`, an approved HTTPS `/v1` endpoint and a credential alias. No model download or cloud provisioning is triggered by creating a runtime record.

## Choose a model and language workload

GGUF describes a weight format. The selected model and chat template determine language quality, instruction following, tool calls and structured output. For an initial Russian/English workload, a non-thinking multilingual instruction model such as Qwen2.5-7B-Instruct in Q4_K_M is a candidate to evaluate. Verify the exact GGUF publisher, immutable source revision and license before use; no weights are bundled or downloaded by this pilot. Other natural languages need representative quality checks for that exact model. Programming-language tasks such as Python, JavaScript/TypeScript and SQL likewise depend on model training and may benefit from a coding-specific model.

Reasoning models, including thinking-enabled Qwen variants, can spend a small output budget on internal reasoning before producing a final answer. Verify their chat template, reasoning settings and output budget separately instead of interpreting a truncated tiny probe as a hardware-speed result.

A 4–8B-class model quantized as Q4_K_M is an example starting point, not a certified hardware or quality recommendation. Weight file size alone does not determine required memory: context length, KV cache, parallel requests and runtime buffers also consume RAM/VRAM. Measure the selected model at the intended context and concurrency before choosing a paid GPU instance.

Choose a single GGUF file with an embedded chat template for this pilot. Record its immutable source revision, SHA256, exact byte size, quantization, context and license in the manifest. Verify permission to use and host both the base model and any fine-tune. A checksum establishes artifact identity; it does not establish license rights or model quality.

## Prepare the artifact and credential

Keep the weights outside the Git checkout in a protected directory. The container path is fixed at `/models/model.gguf`; the artifact verifier checks the declared byte size and streams SHA256 before the inference service starts. It also checks the GGUF header. Successful verification is followed by llama.cpp's own model loading and protocol checks.

Use a new versioned model alias for a new artifact. Keep the old endpoint/model version available while queued or resumable runs still reference it. Runtime snapshots freeze model names and endpoint settings; an operator replacing remote weights under an unchanged alias can still change the model seen by those runs.

Place `model.gguf` and a completed `manifest.json` in the model directory. The manifest records these required fields:

| Field | Required identity |
| --- | --- |
| `schema_version` | Integer `1`. |
| `model_alias`, `model_file` | Versioned serving alias and the fixed filename `model.gguf`. |
| `sha256`, `size_bytes` | Expected lowercase SHA256 and exact positive byte size. |
| `source.url`, `source.revision` | HTTPS source URL and immutable 40- or 64-character lowercase hexadecimal revision. |
| `license.identifier`, `license.url` | Declared license identifier and HTTPS license reference. |
| `quantization`, `context_size` | Quantization identity and configured context, for example `Q4_K_M` and `4096`. |
| `chat_template`, `languages` | `embedded` for this pilot and declared language tags such as `["ru", "en"]`. |

Copy [`manifest.example.json`](../deploy/gguf/manifest.example.json) into the model directory as `manifest.json` and fill every placeholder, including the zero byte size. The unedited example deliberately fails verification. Use a trusted publisher checksum when available and compare the downloaded bytes. A locally computed checksum records which bytes you have; the source/license/language fields remain operator declarations.

The independent verifier can also run on the host:

```sh
python src/gguf/artifacts.py \
  --manifest /absolute/private/models/manifest.json \
  --model /absolute/private/models/model.gguf \
  --alias qwen-pilot-v1 --context-size 4096
```

Generate a dedicated random API credential into a new protected file, without printing or overwriting it:

```sh
umask 077
python - <<'PY'
import os
import secrets

descriptor = os.open("/absolute/private/gguf-api-key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as stream:
    stream.write(secrets.token_urlsafe(32) + "\n")
PY
```

Replace the path with a new file in a directory you control. Keep its value out of Git, command arguments and application runtime records. The llama.cpp server reads the credential through a read-only secret-file mount. The platform's chosen Secret Manager alias must resolve to the same accepted credential. Publish the token characters without the key file's trailing newline when storing that value in Secret Manager.

## Start and validate the portable server

Use the deployment files under [`deploy/gguf`](../deploy/gguf). The base configuration binds TLS to loopback and uses a private Caddy CA. The inference port stays on the private Docker network; public requests are restricted to the supported `/v1` API paths. Health, properties, slots, metrics and built-in tool/agent interfaces are not published through the proxy.

Configure the required model, manifest, key-file and alias paths before starting. Do not bypass the artifact-verification dependency. The GPU override requires a compatible NVIDIA driver and container runtime on the host; a Compose `config` check does not verify that hardware.

From the repository root, copy the pilot environment example once:

```sh
cp -n deploy/gguf/.env.example deploy/gguf/.env
id -u
id -g
```

Edit `deploy/gguf/.env`: set absolute `GGUF_MODEL_DIR` and `GGUF_API_KEY_FILE` paths, the manifest's `GGUF_MODEL_ALIAS` and `GGUF_CONTEXT_SIZE`, and the non-root file owner's numeric `GGUF_UID`/`GGUF_GID`. The server must be able to read the mode-0600 key; use a dedicated non-root owner when operating from a root shell, rather than making the credential world-readable. The local example uses `GGUF_BIND_ADDRESS=127.0.0.1` and `GGUF_HTTPS_PORT=8443`.

If the host shell reports UID `0`, choose positive service IDs, for example `GGUF_UID=10001` and `GGUF_GID=10001`, instead of copying the root IDs into the configuration. Give only the dedicated pilot key file to those IDs:

```sh
chown 10001:10001 /absolute/private/gguf-api-key
chmod 0600 /absolute/private/gguf-api-key
```

The chosen UID also needs search permission on the key file's parent directories and read/search access to the dedicated model directory and its files. Arrange those permissions on the pilot paths; do not recursively change ownership of the repository or unrelated host directories. The Compose initialization service prepares ownership of its named state volumes.

Validate the expanded CPU configuration and start the isolated project:

```sh
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml config --quiet
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml up -d --force-recreate
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml ps
```

For an NVIDIA host, add `-f deploy/gguf/compose.gpu.yaml` to each command before `config`, `up` or `ps`. It selects the pinned CUDA image and GPU reservation; host driver/toolkit compatibility must be verified on that host. Keep this pilot's project name separate from the platform's own Compose project.

The model verifier must complete successfully before llama.cpp starts. The supported start/restart command uses `--force-recreate` so an earlier successful verifier container is not reused after artifact or key-file changes. A bare `docker compose restart` does not re-run dependency checks. On every server start, the startup wrapper also compares the current artifact, alias and context with the verifier's receipt and snapshots a validated key file; mismatches stop startup. Retain immutable model directories and re-run the supported procedure for every controlled restart.

Inspect the preflight and private health status when diagnosing startup:

```sh
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml run --rm --no-deps verify-model
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml exec -T llama \
  curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8080/health
```

Loading failure or an invalid manifest must be fixed before treating the endpoint as ready. The HTTP health address is private to the inference container and is not an application runtime URL.

The separate `deploy/gguf/compose.cloud.yaml` TLS override is intended for a later deployment with an operator-controlled hostname, DNS and certificate issuance. Set `GGUF_HOSTNAME` to that public DNS name, `GGUF_BIND_ADDRESS=0.0.0.0` and `GGUF_HTTPS_PORT=443`; add the cloud override after the CPU base and optional GPU override. It uses ACME and publishes HTTP port 80 for certificate handling. Review the actual instance type, region, disk, firewall, price and shutdown plan before applying this configuration to a paid host.

## Probe capabilities and measure requests

Run the pilot commands from a client that trusts the endpoint's TLS certificate. For the local Caddy configuration, export its CA certificate and pass it explicitly as the probe's CA file. Certificate verification remains enabled.

After the proxy starts, export only the public local CA certificate:

```sh
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml exec -T proxy \
  cat /data/caddy/pki/authorities/local/root.crt > /absolute/private/gguf-root.crt
```

Use the hostname configured in `GGUF_HOSTNAME` in every client URL. If a custom local hostname does not resolve through the operating system's resolver, configure it to resolve to loopback before probing; a hostname must also match the issued certificate.

Read the pinned image identity from the same expanded configuration used for the server:

```sh
GGUF_SERVER_IMAGE="$(docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml config --format json \
  | python -c 'import json, sys; print(json.load(sys.stdin)["services"]["llama"]["image"])')"
```

Include the GPU override in this image lookup when using CUDA. Set the endpoint hostname below to the chosen local `GGUF_HOSTNAME`, and use a single-key client file whose value the server currently accepts:

```sh
uv run --frozen python -m src.gguf.pilot probe \
  --manifest /absolute/private/models/manifest.json \
  --base-url https://localhost:8443/v1 \
  --api-key-file /absolute/private/gguf-api-key \
  --ca-file /absolute/private/gguf-root.crt \
  --server-image "$GGUF_SERVER_IMAGE" \
  --timeout 30 --deadline 120 --max-output-tokens 64 \
  --output /absolute/private/gguf-probe.json
```

Use a fresh output path for each run. Add `--check-tools` or `--check-structured` to explicitly test those features. Streaming remains disabled until a separate verified streaming check is available. To also write a candidate runtime configuration, add `--runtime-output /absolute/private/gguf-runtime.json --secret-ref gguf-pilot --owner-id 42`; inspect it before submitting it through the platform's staff runtime API.

Readiness includes the expected model alias appearing in `/v1/models` and a successful authenticated completion. The probe also checks that missing and incorrect credentials are rejected. The model must be loaded and serving requests before the result establishes readiness.

Begin with tool, structured-output and streaming capabilities disabled in the platform runtime. Enable a feature only after a corresponding probe passes for the exact weights, quantization, chat template, context and server build. A successful basic text completion does not certify tool calling or JSON Schema support.

The benchmark records request wall latency and reported token usage. Completion tokens divided by request wall time includes prompt processing and overhead; it is not a separate decoding-speed or first-token measurement. Missing usage stays unknown. Compare representative Russian/English answers separately: protocol success does not establish answer quality.

GPU cost estimates need the instance's actual hourly price and total billable lifetime, including startup, warm-up and idle time. Token tariffs from an unrelated cloud API do not describe a self-hosted GPU's cost. When price or lifetime is unknown, keep cost unknown and record the missing input. Include persistent disk, data transfer and provider billing increments in the final hosting comparison.

For a measured host, a bounded benchmark can use:

```sh
uv run --frozen python -m src.gguf.pilot benchmark \
  --manifest /absolute/private/models/manifest.json \
  --base-url https://localhost:8443/v1 \
  --api-key-file /absolute/private/gguf-api-key \
  --ca-file /absolute/private/gguf-root.crt \
  --server-image "$GGUF_SERVER_IMAGE" \
  --requests 3 --max-output-tokens 128 --timeout 30 --deadline 120 \
  --hourly-price ACTUAL_HOURLY_PRICE --billable-seconds ACTUAL_INSTANCE_LIFETIME \
  --currency USD --output /absolute/private/gguf-benchmark.json
```

Replace the price/lifetime placeholders with actual numeric values and use the instance's billing currency. Omit unknown cost inputs; the report should then retain an unknown cost rather than fabricate one. The request count is bounded at 20. A full experiment can need a larger declared deadline than this short example, particularly on CPU; never remove the request/time limits merely to obtain a successful measurement.

Record the artifact SHA, source revision, model alias, quantization, context, chat-template identity and pinned server image with each result. The report's artifact metadata is an operator declaration unless the client has independently verified the same local file; it is not remote attestation of the server's weights.

## Connect the platform runtime

Add the exact TLS endpoint to the server's allowlist and configure an alias through the existing [Secret Manager integration](secret-manager.md):

```dotenv
LLM_ALLOWED_BASE_URLS=["https://api.openai.com/v1","https://gguf.example.com/v1"]
```

Then create a staff-managed runtime for the intended owner, using the exact alias served by llama.cpp:

```json
{
  "owner": 42,
  "name": "Versioned GGUF pilot",
  "profile": "openai_compatible",
  "base_url": "https://gguf.example.com/v1",
  "secret_ref": "gguf-pilot",
  "allowed_models": ["qwen-pilot-v1"],
  "capabilities": {
    "tools": false,
    "strict_tools": false,
    "structured_outputs": false,
    "streaming": false,
    "usage": false,
    "temperature": false,
    "token_limit_parameter": "max_tokens"
  },
  "enabled": true
}
```

Replace the owner ID, endpoint and model alias. Preserve the verified TLS policy; llama.cpp uses the OpenAI-compatible profile, not the Ollama-specific plain-HTTP exception. Set only capabilities supported by the completed probes. Assign the runtime to an owned active agent and set its `model_name` to the same alias.

Inside a web or worker container, `localhost` identifies that container. The worker must reach the TLS proxy through its deployment network and trust its CA. An endpoint that worked from a developer shell may not be reachable or trusted from the worker. Public ACME certificates generally need no private CA configuration; private certificates require installing the CA in the calling process's trusted certificate bundle while retaining normal certificate and hostname checks.

Direct pilot probes call llama.cpp independently and do not enter the platform's workflow ledger. Finish the pilot by creating a workflow through the platform, executing it with the real outbox worker, and inspecting its events and `/api/v1/llm-calls/?run_id=RUN_UUID` records. Check usage, owner isolation, quota denial, timeout/cancellation and any required tools or structured result schema through that path. [LLM accounting](llm-usage.md) describes reservations and unknown usage.

## Rotate keys, stop and resume

The application can adopt a changed Secret Manager value after its configured TTL. Native llama.cpp API-key files are loaded by the server at startup; changing the platform alias or the mounted file alone does not reload the server's accepted credentials.

The server file supports up to four distinct keys, one per line. A probe client file must contain just the key being tested. For rotation with overlap:

1. Place both the old and new random keys in the protected server file, then use the controlled `up -d --force-recreate` procedure. Probe the new key before updating the application alias.
2. Publish the new key through the existing Secret Manager alias. Keep both server keys valid until application caches refresh and existing requests drain.
3. Remove the old key from the server file and redeploy again. Confirm that the new key succeeds and the retired key is rejected.

A single-instance server restart can interrupt inference; use a separately designed redundant deployment if uninterrupted service is required. Follow the application-side [rotation procedure](secret-manager.md) as well.

Stop the pilot's Compose project when measurements are complete. Keep model files and manifests for reproducibility; avoid volume-deletion commands as a routine stop operation. A local Compose stop does not stop a separately provisioned cloud VM or its disk billing. Confirm provider resources are stopped or deleted through the chosen provider's own controls when a real cloud pilot is later run.

```sh
docker compose -p gguf-pilot --env-file deploy/gguf/.env \
  -f deploy/gguf/compose.yaml stop
```

Include the same overrides used at startup. The read-only model files and Caddy state volumes remain available for a later controlled restart.

The current portable work does not establish real GPU throughput, cold-start time or cloud cost. Those measurements require the selected licensed model on the intended hardware and provider plan. Model upload, GPU provisioning, autoscaling and hosting subscription integration remain deployment-specific follow-up work.
