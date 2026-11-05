"""Native GGUF probes are bounded and never claim unmeasured capabilities/cost."""

import asyncio
import copy
import json
import os
import stat

import httpx
import pytest

from src.gguf import pilot

KEY = "private-pilot-auth-key"
ALIAS = "qwen-ru-en-q4km-v1"
IMAGE = "ghcr.io/ggml-org/llama.cpp:server@sha256:" + "a" * 64
MANIFEST = {
    "model_alias": ALIAS,
    "sha256": "b" * 64,
    "source": {"url": "https://example.com/model.gguf", "revision": "c" * 40},
    "license": {"identifier": "Apache-2.0", "url": "https://example.com/license"},
    "quantization": "Q4_K_M",
    "context_size": 4096,
    "chat_template": "embedded",
    "languages": ["ru", "en"],
}


def completion(
    content="private-model-output", *, calls=None, finish="stop", usage=True, model=ALIAS
):
    body = {
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    return body


@pytest.fixture
def config():
    return pilot.PilotConfig("https://localhost:8443/v1/", IMAGE)


@pytest.fixture
def server():
    received, customized = [], []

    async def respond(request):
        received.append(request)
        if customized:
            result = customized.pop(0)
            return await result(request) if callable(result) else result
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if request.headers.get("authorization") != "Bearer " + KEY:
            return httpx.Response(401, json={"error": KEY})
        body = json.loads(request.content)
        if "response_format" in body:
            result = completion('{"approved":true,"score":0.5}')
        elif "tools" in body:
            result = completion(
                None,
                calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "pilot_echo", "arguments": '{"text":"ping"}'},
                    }
                ],
                finish="tool_calls",
            )
        else:
            result = completion()
        return httpx.Response(200, json=result)

    return received, customized, httpx.MockTransport(respond)


async def test_probe_records_only_observed_capabilities_and_public_identity(config, server):
    requests, _, transport = server
    report = await pilot.probe(config, MANIFEST, api_key=KEY, transport=transport)
    assert report["status"] == "passed" and report["http_requests"] == 4
    assert report["artifact"]["sha256"] == MANIFEST["sha256"]
    assert report["artifact_verification"] == "manifest_declaration_only"
    assert report["gpu_verification"] == report["quality_evaluation"] == "not_performed"
    assert report["workflow_accounting"] == "direct_requests_not_in_workflow_ledger"
    assert report["capabilities"] == {
        "tools": False,
        "structured_outputs": False,
        "streaming": False,
        "usage": True,
        "strict_tools": False,
        "temperature": False,
        "token_limit_parameter": "max_tokens",
    }
    encoded = json.dumps(report)
    assert KEY not in encoded and "private-model-output" not in encoded
    assert "authorization" not in requests[1].headers
    assert json.loads(requests[1].content)["max_tokens"] == 1
    assert requests[2].headers["authorization"] != "Bearer " + KEY
    assert requests[3].headers["authorization"] == "Bearer " + KEY


async def test_structured_and_tool_probes_enable_only_checked_features(config, server):
    report = await pilot.probe(
        config, MANIFEST, api_key=KEY, transport=server[2], check_structured=True, check_tools=True
    )
    assert report["http_requests"] == 6
    assert report["capabilities"]["tools"] is report["capabilities"]["structured_outputs"] is True
    assert report["capabilities"]["strict_tools"] is False
    assert [sample["kind"] for sample in report["samples"]] == ["completion", "structured", "tools"]
    candidate = pilot.build_runtime_payload(report, "gguf-pilot", owner_id=42)
    assert candidate["profile"] == "openai_compatible"
    assert candidate["allowed_models"] == [ALIAS] and candidate["owner"] == 42
    assert candidate["base_url"] == "https://localhost:8443/v1"
    assert candidate["secret_ref"] == "gguf-pilot"
    assert KEY not in json.dumps(candidate)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/v1",
        "https://user:secret@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#fragment",
        "https://host/other/v1",
        "https://host/%2f/v1",
        "https://host/v1?",
        "https://host/v1#",
        "https://host\\private/v1",
        "https://host/v1\n",
    ],
)
def test_endpoint_rejects_plaintext_or_ambiguous_authentication_routes(url):
    with pytest.raises(pilot.PilotError, match="invalid_https_endpoint"):
        pilot.PilotConfig(url, IMAGE)


@pytest.mark.parametrize(
    "changes",
    [
        {"server_image": "llama:latest"},
        {"timeout": 0},
        {"timeout": 121},
        {"deadline": 601},
        {"deadline": float("inf")},
        {"max_output_tokens": 0},
        {"max_output_tokens": 513},
        {"max_output_tokens": True},
    ],
)
def test_configuration_requires_pinned_bounded_execution(changes):
    with pytest.raises(pilot.PilotError):
        pilot.PilotConfig(
            **{"base_url": "https://example.com/v1", "server_image": IMAGE, **changes}
        )


async def test_model_alias_must_be_ready_before_any_completion(config, server):
    server[1].append(httpx.Response(200, json={"data": [{"id": "other"}]}))
    with pytest.raises(pilot.PilotError, match="model_alias_not_ready"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    assert len(server[0]) == 1


async def test_unauthenticated_completion_must_be_rejected(config, server):
    server[1].extend(
        [
            httpx.Response(200, json={"data": [{"id": ALIAS}]}),
            httpx.Response(200, json=completion()),
        ]
    )
    with pytest.raises(pilot.PilotError, match="auth_not_enforced"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    assert len(server[0]) == 2


async def test_any_nonempty_bearer_is_not_sufficient_authentication(config):
    seen = []

    def insecure(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if "authorization" not in request.headers:
            return httpx.Response(401)
        return httpx.Response(200, json=completion())

    with pytest.raises(pilot.PilotError, match="auth_not_enforced"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=httpx.MockTransport(insecure))
    assert len(seen) == 3
    assert seen[-1].headers["authorization"] != "Bearer " + KEY


async def test_redirect_does_not_forward_credentials(config, server):
    server[1].append(httpx.Response(302, headers={"Location": "https://other.example/v1/models"}))
    with pytest.raises(pilot.PilotError, match="unexpected_http_status"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    assert len(server[0]) == 1


async def run_custom_completion(config, body, **options):
    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if request.headers.get("authorization") != "Bearer " + KEY:
            return httpx.Response(401)
        return httpx.Response(200, json=body)

    return await pilot.probe(
        config, MANIFEST, api_key=KEY, transport=httpx.MockTransport(handle), **options
    )


@pytest.mark.parametrize(
    "body,code",
    [
        (completion(finish="length"), "output_limit_reached"),
        (completion(model="different-model"), "unexpected_response_model"),
        ({"model": ALIAS, "choices": []}, "invalid_completion"),
        (completion(content=None), "invalid_completion"),
        (completion(calls=[{"id": "unexpected"}]), "invalid_completion"),
    ],
)
async def test_invalid_completion_cannot_produce_success_report(config, body, code):
    with pytest.raises(pilot.PilotError, match=code):
        await run_custom_completion(config, body)


@pytest.mark.parametrize(
    "content",
    [
        '{"approved":"private-marker","score":0.5}',
        '{"approved":true,"score":2}',
        '{"approved":true,"score":0.5,"extra":"private-marker"}',
        "[]",
        '{"approved":true,"score":NaN}',
        '{"approved":true,"score":true}',
    ],
)
async def test_structured_probe_rejects_wrong_schema_without_echo(config, content):
    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if request.headers.get("authorization") != "Bearer " + KEY:
            return httpx.Response(401)
        payload = completion(
            content if "response_format" in json.loads(request.content) else "READY"
        )
        return httpx.Response(200, json=payload)

    with pytest.raises(pilot.PilotError) as error:
        await pilot.probe(
            config,
            MANIFEST,
            api_key=KEY,
            check_structured=True,
            transport=httpx.MockTransport(handle),
        )
    assert "private-marker" not in str(error.value)


async def test_tool_probe_rejects_wrong_arguments_and_never_executes_functions(config):
    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if request.headers.get("authorization") != "Bearer " + KEY:
            return httpx.Response(401)
        if "tools" in json.loads(request.content):
            body = completion(
                None,
                calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "pilot_echo", "arguments": '{"text":"wrong"}'},
                    }
                ],
                finish="tool_calls",
            )
        else:
            body = completion()
        return httpx.Response(200, json=body)

    with pytest.raises(pilot.PilotError, match="tool_probe_failed"):
        await pilot.probe(
            config, MANIFEST, api_key=KEY, check_tools=True, transport=httpx.MockTransport(handle)
        )


async def test_missing_usage_remains_unknown_and_throughput_is_not_fabricated(config):
    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": ALIAS}]})
        if request.headers.get("authorization") != "Bearer " + KEY:
            return httpx.Response(401)
        return httpx.Response(200, json=completion(usage=False))

    report = await pilot.benchmark(
        config, MANIFEST, api_key=KEY, requests=2, transport=httpx.MockTransport(handle)
    )
    assert report["capabilities"]["usage"] is False
    assert report["benchmark"]["reported_tokens"] == dict.fromkeys(pilot._TOKEN_FIELDS)
    assert report["benchmark"]["completion_tokens_per_request_wall_second"] is None
    assert report["cost"]["compute_cost_estimate"] is None


async def test_benchmark_uses_explicit_instance_lifetime_and_decimal_price(config, server):
    report = await pilot.benchmark(
        config,
        MANIFEST,
        api_key=KEY,
        requests=3,
        hourly_price="0.50",
        billable_seconds="7200",
        transport=server[2],
    )
    assert report["http_requests"] == 6
    assert report["benchmark"]["reported_tokens"] == {
        "prompt_tokens": 15,
        "completion_tokens": 6,
        "total_tokens": 21,
    }
    assert report["cost"]["compute_cost_estimate"] == "1.00000000"
    assert report["cost"]["billable_instance_seconds"] == "7200"
    assert "not_decode_rate_or_TTFT" in report["benchmark"]["throughput_basis"]


async def test_price_without_instance_lifetime_does_not_assume_benchmark_time(config, server):
    report = await pilot.benchmark(
        config, MANIFEST, api_key=KEY, requests=1, hourly_price="2.00", transport=server[2]
    )
    assert report["cost"]["compute_cost_estimate"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"requests": 0},
        {"requests": 21},
        {"requests": True},
        {"hourly_price": "NaN"},
        {"hourly_price": "-1"},
        {"billable_seconds": "Infinity"},
        {"currency": "usd"},
    ],
)
async def test_benchmark_limits_are_checked_before_http(config, server, options):
    with pytest.raises(pilot.PilotError):
        await pilot.benchmark(config, MANIFEST, api_key=KEY, transport=server[2], **options)
    assert server[0] == []


async def test_total_deadline_stops_slow_stream_and_closes_it():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.03)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    config = pilot.PilotConfig("https://example.com/v1", IMAGE, deadline=0.1)
    with pytest.raises(pilot.PilotError, match="deadline_exceeded"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=transport)
    assert stream.closed


async def test_oversized_response_and_vendor_error_are_sanitized(config):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 64001))
    with pytest.raises(pilot.PilotError, match="response_too_large"):
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=transport)

    def fail(request):
        raise RuntimeError("vendor-body-" + KEY)

    with pytest.raises(pilot.PilotError) as error:
        await pilot.probe(config, MANIFEST, api_key=KEY, transport=httpx.MockTransport(fail))
    assert KEY not in str(error.value)


def test_key_file_has_one_printable_key_and_no_symlink_or_fifo(tmp_path):
    path = tmp_path / "key"
    path.write_text(KEY + "\n")
    assert pilot.read_api_key(path) == KEY
    path.write_text(KEY + "\nsecond-key\n")
    with pytest.raises(pilot.PilotError):
        pilot.read_api_key(path)
    path.write_text(KEY)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(pilot.PilotError):
        pilot.read_api_key(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(pilot.PilotError):
        pilot.read_api_key(fifo)


def test_report_file_is_private_and_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "report.json"
    pilot.write_json(path, {"status": "passed"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(pilot.PilotError):
        pilot.write_json(path, {"status": "changed"})
    assert json.loads(path.read_text()) == {"status": "passed"}


@pytest.mark.parametrize("capability", ["streaming", "strict_tools", "temperature"])
async def test_runtime_candidate_cannot_enable_an_untested_capability(config, server, capability):
    report = await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    changed = copy.deepcopy(report)
    changed["capabilities"][capability] = True
    with pytest.raises(pilot.PilotError, match="untested_probe_capability"):
        pilot.build_runtime_payload(changed, "gguf-key")


async def test_benchmark_report_is_not_runtime_acceptance_evidence(config, server):
    report = await pilot.benchmark(config, MANIFEST, api_key=KEY, transport=server[2])
    with pytest.raises(pilot.PilotError, match="successful_probe_required"):
        pilot.build_runtime_payload(report, "gguf-key")


async def test_runtime_candidate_requires_evidence_for_checked_model_features(config, server):
    report = await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    report["capabilities"]["tools"] = True
    with pytest.raises(pilot.PilotError, match="invalid_probe_evidence"):
        pilot.build_runtime_payload(report, "gguf-key")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(samples=[]),
        lambda report: report["samples"].append(copy.deepcopy(report["samples"][0])),
        lambda report: report["samples"][0].update(kind=[]),
        lambda report: report["samples"][0].update(usage=dict.fromkeys(pilot._TOKEN_FIELDS)),
        lambda report: report["samples"][0]["usage"].update(total_tokens=-1),
        lambda report: report["samples"][0].update(elapsed_seconds=float("nan")),
        lambda report: report["samples"][0].update(unexpected="data"),
        lambda report: report["authentication"].update(wrong_key_rejected=False),
    ],
)
async def test_partial_or_corrupted_evidence_cannot_enable_runtime_capabilities(
    config, server, mutation
):
    report = await pilot.probe(config, MANIFEST, api_key=KEY, transport=server[2])
    mutation(report)
    with pytest.raises(pilot.PilotError, match="invalid_probe_evidence"):
        pilot.build_runtime_payload(report, "gguf-key")


def test_cli_errors_do_not_echo_file_or_vendor_secret(tmp_path, capsys):
    assert (
        pilot.main(
            [
                "probe",
                "--manifest",
                str(tmp_path / KEY),
                "--base-url",
                "http://invalid/v1",
                "--api-key-file",
                str(tmp_path / KEY),
                "--server-image",
                IMAGE,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err


@pytest.mark.parametrize("option", ["--timeout", "--requests", "--owner-id"])
def test_invalid_numeric_cli_arguments_are_sanitized(option, capsys):
    assert (
        pilot.main(
            [
                "probe",
                "--manifest",
                "manifest.json",
                "--base-url",
                "https://example.com/v1",
                "--api-key-file",
                "key.txt",
                "--server-image",
                IMAGE,
                option,
                KEY,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "failed",
        "error_code": "invalid_pilot_arguments",
    }
    assert KEY not in captured.err
