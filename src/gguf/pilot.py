"""Bounded HTTPS probes and wall-time benchmarks for an operator-owned GGUF server.

These direct requests do not enter the workflow invocation ledger. A report
records declared artifact identity; it does not attest remote weights or GPUs.
"""

import argparse
import asyncio
import copy
import json
import math
import os
import re
import secrets
import ssl
import stat
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:-]*@sha256:[a-f0-9]{64}$")
_MAX_RESPONSE_BYTES = 64_000
_MAX_REPORT_BYTES = 256_000
_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_PROMPT = "Ответь одним словом READY. Reply with the single word READY."


class PilotError(ValueError):
    """Public, fixed error codes; vendor bodies and credentials never escape."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"GGUF pilot failed ({code})")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise PilotError("invalid_pilot_arguments")


def normalize_base_url(value: Any) -> str:
    try:
        if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\x7f\\]", value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
            or "?" in value
            or "#" in value
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and port != 443:
            if not 1 <= port <= 65535:
                raise ValueError
            host += f":{port}"
        return urlunsplit(("https", host, "/v1", "", ""))
    except (ValueError, UnicodeError):
        raise PilotError("invalid_https_endpoint") from None


def _bounded_number(value: Any, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


@dataclass(frozen=True)
class PilotConfig:
    base_url: str
    server_image: str
    timeout: float = 30
    deadline: float = 120
    max_output_tokens: int = 64

    def __post_init__(self):
        object.__setattr__(self, "base_url", normalize_base_url(self.base_url))
        if not isinstance(self.server_image, str) or _IMAGE.fullmatch(self.server_image) is None:
            raise PilotError("server_image_requires_digest")
        if not _bounded_number(self.timeout, 0.1, 120):
            raise PilotError("invalid_timeout")
        if not _bounded_number(self.deadline, 0.1, 600):
            raise PilotError("invalid_deadline")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 512:
            raise PilotError("invalid_output_bound")


def _validate_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16_384
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise PilotError("invalid_api_key")
    return value


def read_api_key(path: str | Path) -> str:
    """Read one regular, non-symlink credential file without echoing its path."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PilotError("invalid_api_key_file")
        raw = os.read(descriptor, 16_387)
        if len(raw) > 16_386:
            raise PilotError("invalid_api_key_file")
        value = raw.decode("ascii")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        return _validate_key(value)
    except (OSError, UnicodeError):
        raise PilotError("invalid_api_key_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        # JSON numeric overflow (1e999) also must not become an infinite metric.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PilotError("invalid_json_response") from None


def _ssl_context(ca_file: str | Path | None) -> ssl.SSLContext:
    descriptor = None
    try:
        if ca_file is None:
            return ssl.create_default_context()
        descriptor = os.open(ca_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError
        data = os.read(descriptor, 1_000_001)
        if len(data) > 1_000_000:
            raise ValueError
        return ssl.create_default_context(cadata=data.decode("ascii"))
    except (OSError, ValueError, UnicodeError):
        raise PilotError("invalid_ca_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class _Requests:
    def __init__(self, client: httpx.AsyncClient, config: PilotConfig, key: str):
        self.client, self.config, self.key = client, config, key
        self.count = 0

    async def request(
        self,
        method: str,
        path: str,
        *,
        body=None,
        authenticated=True,
        statuses=(200,),
        bearer_override: str | None = None,
        failure_code: str = "unexpected_http_status",
    ):
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            headers["Authorization"] = "Bearer " + (bearer_override or self.key)
        self.count += 1
        try:
            async with self.client.stream(
                method, self.config.base_url + path, headers=headers, json=body
            ) as response:
                if response.status_code not in statuses:
                    raise PilotError(failure_code)
                if response.status_code != 200:
                    return None
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise PilotError("encoded_response_not_permitted")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise PilotError("response_too_large")
                    chunks.append(chunk)
                return _json_object(b"".join(chunks))
        except httpx.HTTPError:
            raise PilotError("transport_error") from None


def _usage(value: Any) -> dict[str, int | None]:
    value = value if isinstance(value, dict) else {}
    result = {
        field: count if type(count := value.get(field)) is int and 0 <= count < 2**63 else None
        for field in _TOKEN_FIELDS
    }
    if result["total_tokens"] is not None and result["total_tokens"] < (
        (result["prompt_tokens"] or 0) + (result["completion_tokens"] or 0)
    ):
        result["total_tokens"] = None
    return result


def _message(payload: Any, alias: str, *, tools=False) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("model") != alias:
        raise PilotError("unexpected_response_model")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise PilotError("invalid_completion")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise PilotError("output_limit_reached")
    message = choice.get("message")
    if (
        choice.get("finish_reason") != ("tool_calls" if tools else "stop")
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
        or message.get("refusal")
    ):
        raise PilotError("invalid_completion")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise PilotError("invalid_completion")
    if not tools and (
        message.get("tool_calls") not in (None, []) or not content or not content.strip()
    ):
        raise PilotError("invalid_completion")
    return message


async def _preflight(requests: _Requests, alias: str) -> None:
    models = await requests.request("GET", "/models")
    data = models.get("data") if isinstance(models, dict) else None
    if not isinstance(data, list) or not any(
        isinstance(item, dict) and item.get("id") == alias for item in data
    ):
        raise PilotError("model_alias_not_ready")
    await requests.request(
        "POST",
        "/chat/completions",
        authenticated=False,
        statuses=(401, 403),
        failure_code="auth_not_enforced",
        body={"model": alias, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
    )
    wrong_key = secrets.token_urlsafe(32)
    if wrong_key == requests.key:
        wrong_key = ("X" if wrong_key[0] != "X" else "Y") + wrong_key[1:]
    await requests.request(
        "POST",
        "/chat/completions",
        bearer_override=wrong_key,
        statuses=(401, 403),
        failure_code="auth_not_enforced",
        body={"model": alias, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
    )


async def _completion(requests: _Requests, alias: str, *, kind="completion") -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": alias,
        "messages": [{"role": "user", "content": _PROMPT}],
        "max_tokens": requests.config.max_output_tokens,
        "stream": False,
    }
    if kind == "structured":
        body["messages"][0]["content"] = 'Return exactly {"approved":true,"score":0.5}.'
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "PilotDecision",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "approved": {"type": "boolean"},
                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["approved", "score"],
                    "additionalProperties": False,
                },
            },
        }
    elif kind == "tools":
        body["messages"][0]["content"] = 'Call pilot_echo once with text="ping".'
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "pilot_echo",
                    "description": "A validation-only test; never executed.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        body["tool_choice"] = {"type": "function", "function": {"name": "pilot_echo"}}
    started = time.perf_counter()
    payload = await requests.request("POST", "/chat/completions", body=body)
    elapsed = time.perf_counter() - started
    message = _message(payload, alias, tools=kind == "tools")
    assert isinstance(payload, dict)
    if kind == "structured":
        value = _json_object(message["content"].encode("utf-8"))
        if (
            set(value) != {"approved", "score"}
            or type(value["approved"]) is not bool
            or not _bounded_number(value["score"], 0, 1)
        ):
            raise PilotError("structured_probe_failed")
    if kind == "tools":
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            raise PilotError("tool_probe_failed")
        call = calls[0]
        function = call.get("function")
        if (
            call.get("type") != "function"
            or not isinstance(call.get("id"), str)
            or _ALIAS.fullmatch(call["id"]) is None
            or not isinstance(function, dict)
            or function.get("name") != "pilot_echo"
            or not isinstance(function.get("arguments"), str)
        ):
            raise PilotError("tool_probe_failed")
        arguments = _json_object(function["arguments"].encode("utf-8"))
        if arguments != {"text": "ping"}:
            raise PilotError("tool_probe_failed")
    return {
        "kind": kind,
        "elapsed_seconds": round(elapsed, 6),
        "usage": _usage(payload.get("usage")),
    }


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise PilotError("invalid_manifest_identity")
    alias, digest = manifest.get("model_alias"), manifest.get("sha256")
    if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
        raise PilotError("invalid_manifest_identity")
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise PilotError("invalid_manifest_identity")
    return copy.deepcopy(
        {
            key: manifest[key]
            for key in (
                "model_alias",
                "sha256",
                "source",
                "license",
                "quantization",
                "context_size",
                "chat_template",
                "languages",
            )
            if key in manifest
        }
    )


def _report(config: PilotConfig, manifest: dict[str, Any], command: str, samples, count: int):
    known_usage = all(
        all(sample["usage"][field] is not None for field in _TOKEN_FIELDS) for sample in samples
    )
    return {
        "schema_version": 1,
        "command": command,
        "status": "passed",
        "base_url": config.base_url,
        "artifact": _manifest_identity(manifest),
        "artifact_verification": "manifest_declaration_only",
        "server_image": config.server_image,
        "server_image_verification": "operator_supplied_digest",
        "workflow_accounting": "direct_requests_not_in_workflow_ledger",
        "quality_evaluation": "not_performed",
        "gpu_verification": "not_performed",
        "http_requests": count,
        "authentication": {
            "missing_key_rejected": True,
            "wrong_key_rejected": True,
            "authenticated_completion_succeeded": True,
        },
        "max_output_tokens": config.max_output_tokens,
        "samples": samples,
        "capabilities": {
            "tools": False,
            "structured_outputs": False,
            "streaming": False,
            "usage": known_usage,
            "strict_tools": False,
            "temperature": False,
            "token_limit_parameter": "max_tokens",
        },
    }


async def probe(
    config: PilotConfig,
    manifest: dict[str, Any],
    *,
    api_key: str,
    check_structured: bool = False,
    check_tools: bool = False,
    ca_file: str | Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    identity, key = _manifest_identity(manifest), _validate_key(api_key)
    if type(check_structured) is not bool or type(check_tools) is not bool:
        raise PilotError("invalid_probe_options")
    try:
        async with asyncio.timeout(config.deadline):
            async with httpx.AsyncClient(
                verify=_ssl_context(ca_file),
                timeout=config.timeout,
                follow_redirects=False,
                transport=transport,
            ) as client:
                requests = _Requests(client, config, key)
                await _preflight(requests, identity["model_alias"])
                samples = [await _completion(requests, identity["model_alias"])]
                if check_structured:
                    samples.append(
                        await _completion(requests, identity["model_alias"], kind="structured")
                    )
                if check_tools:
                    samples.append(
                        await _completion(requests, identity["model_alias"], kind="tools")
                    )
                report = _report(config, manifest, "probe", samples, requests.count)
                report["capabilities"].update(
                    tools=check_tools, structured_outputs=check_structured
                )
                return report
    except TimeoutError:
        raise PilotError("deadline_exceeded") from None
    except PilotError:
        raise
    except Exception:
        raise PilotError("pilot_transport_failed") from None


def _decimal(value: Decimal | str | None, name: str, maximum: Decimal) -> Decimal | None:
    if value is None:
        return None
    try:
        if not isinstance(value, (str, Decimal)):
            raise ValueError
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed < 0 or parsed > maximum:
            raise ValueError
        return parsed
    except (InvalidOperation, ValueError, TypeError):
        raise PilotError("invalid_" + name) from None


async def benchmark(
    config: PilotConfig,
    manifest: dict[str, Any],
    *,
    api_key: str,
    requests: int = 3,
    hourly_price: Decimal | str | None = None,
    billable_seconds: Decimal | str | None = None,
    currency: str = "USD",
    ca_file: str | Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    identity, key = _manifest_identity(manifest), _validate_key(api_key)
    if type(requests) is not int or not 1 <= requests <= 20:
        raise PilotError("invalid_request_count")
    price = _decimal(hourly_price, "hourly_price", Decimal("1000000"))
    lifetime = _decimal(billable_seconds, "billable_seconds", Decimal("31536000"))
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise PilotError("invalid_currency")
    try:
        async with asyncio.timeout(config.deadline):
            async with httpx.AsyncClient(
                verify=_ssl_context(ca_file),
                timeout=config.timeout,
                follow_redirects=False,
                transport=transport,
            ) as client:
                calls = _Requests(client, config, key)
                await _preflight(calls, identity["model_alias"])
                samples = [
                    await _completion(calls, identity["model_alias"]) for _ in range(requests)
                ]
                report = _report(config, manifest, "benchmark", samples, calls.count)
    except TimeoutError:
        raise PilotError("deadline_exceeded") from None
    except PilotError:
        raise
    except Exception:
        raise PilotError("pilot_transport_failed") from None
    durations = sorted(sample["elapsed_seconds"] for sample in samples)
    total_wall = sum(durations)
    totals = {
        field: sum(sample["usage"][field] for sample in samples)
        if all(sample["usage"][field] is not None for sample in samples)
        else None
        for field in _TOKEN_FIELDS
    }
    with localcontext() as context:
        context.prec = 40
        cost = (
            price * lifetime / Decimal(3600) if price is not None and lifetime is not None else None
        )
    report["benchmark"] = {
        "requests": requests,
        "latency_seconds": {
            "mean": round(total_wall / requests, 6),
            "p50": durations[math.ceil(requests * 0.50) - 1],
            "p95": durations[math.ceil(requests * 0.95) - 1],
            "percentile_method": "nearest_rank",
        },
        "reported_tokens": totals,
        "completion_tokens_per_request_wall_second": round(
            totals["completion_tokens"] / total_wall, 6
        )
        if totals["completion_tokens"] is not None and total_wall > 0
        else None,
        "throughput_basis": "completion_tokens_over_total_request_wall_time_not_decode_rate_or_TTFT",
    }
    report["cost"] = {
        "currency": currency,
        "hourly_compute_price": str(price) if price is not None else None,
        "billable_instance_seconds": str(lifetime) if lifetime is not None else None,
        "compute_cost_estimate": format(cost, ".8f") if cost is not None else None,
        "basis": "explicit_instance_lifetime_including_startup_and_idle",
        "excludes": ["storage", "network_egress", "tax"],
    }
    return report


def build_runtime_payload(report: dict[str, Any], secret_ref: str, owner_id: int | None = None):
    if (
        type(report.get("schema_version")) is not int
        or report["schema_version"] != 1
        or report.get("command") != "probe"
        or report.get("status") != "passed"
    ):
        raise PilotError("successful_probe_required")
    identity = _manifest_identity(report.get("artifact", {}))
    base_url = normalize_base_url(report.get("base_url"))
    if not isinstance(secret_ref, str) or _ALIAS.fullmatch(secret_ref) is None:
        raise PilotError("invalid_secret_alias")
    if owner_id is not None and (type(owner_id) is not int or owner_id < 1):
        raise PilotError("invalid_owner")
    capabilities = report.get("capabilities")
    names = ("tools", "structured_outputs", "streaming", "usage", "strict_tools", "temperature")
    if not isinstance(capabilities, dict) or any(
        type(capabilities.get(name)) is not bool for name in names
    ):
        raise PilotError("invalid_probe_capabilities")
    if set(capabilities) != {*names, "token_limit_parameter"}:
        raise PilotError("invalid_probe_capabilities")
    if capabilities["streaming"] or capabilities["strict_tools"] or capabilities["temperature"]:
        raise PilotError("untested_probe_capability")
    if capabilities.get("token_limit_parameter") != "max_tokens":
        raise PilotError("invalid_probe_capabilities")
    if report.get("authentication") != {
        "missing_key_rejected": True,
        "wrong_key_rejected": True,
        "authenticated_completion_succeeded": True,
    }:
        raise PilotError("invalid_probe_evidence")
    samples = report.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 3:
        raise PilotError("invalid_probe_evidence")
    kinds = [sample.get("kind") if isinstance(sample, dict) else None for sample in samples]
    if (
        kinds[0] != "completion"
        or any(
            not isinstance(kind, str) or kind not in {"completion", "structured", "tools"}
            for kind in kinds
        )
        or len(set(kinds)) != len(kinds)
        or capabilities["tools"] != ("tools" in kinds)
        or capabilities["structured_outputs"] != ("structured" in kinds)
        or report.get("http_requests") != len(samples) + 3
    ):
        raise PilotError("invalid_probe_evidence")
    for sample in samples:
        if (
            set(sample) != {"kind", "elapsed_seconds", "usage"}
            or not _bounded_number(sample["elapsed_seconds"], 0, 600)
            or not isinstance(sample["usage"], dict)
            or set(sample["usage"]) != set(_TOKEN_FIELDS)
            or _usage(sample["usage"]) != sample["usage"]
        ):
            raise PilotError("invalid_probe_evidence")
    usage_observed = all(
        all(sample["usage"][field] is not None for field in _TOKEN_FIELDS) for sample in samples
    )
    if capabilities["usage"] != usage_observed:
        raise PilotError("invalid_probe_evidence")
    payload = {
        "name": ("GGUF " + identity["model_alias"])[:128],
        "profile": "openai_compatible",
        "base_url": base_url,
        "secret_ref": secret_ref,
        "allowed_models": [identity["model_alias"]],
        "capabilities": copy.deepcopy(capabilities),
        "enabled": True,
    }
    if owner_id is not None:
        payload["owner"] = owner_id
    return payload


def write_json(path: str | Path, value: dict[str, Any]) -> None:
    descriptor = None
    try:
        raw = (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
        if len(raw) > _MAX_REPORT_BYTES:
            raise PilotError("report_too_large")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        raise PilotError("report_not_written") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "benchmark"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--server-image", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--max-output-tokens", type=int, default=64)
    parser.add_argument("--output")
    parser.add_argument("--check-structured", action="store_true")
    parser.add_argument("--check-tools", action="store_true")
    parser.add_argument("--runtime-output")
    parser.add_argument("--secret-ref")
    parser.add_argument("--owner-id", type=int)
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--hourly-price")
    parser.add_argument("--billable-seconds")
    parser.add_argument("--currency", default="USD")
    try:
        args = parser.parse_args(argv)
        from .artifacts import load_manifest

        manifest = load_manifest(args.manifest)
        config = PilotConfig(
            args.base_url, args.server_image, args.timeout, args.deadline, args.max_output_tokens
        )
        key = read_api_key(args.api_key_file)
        if args.command == "probe":
            if args.runtime_output and not args.secret_ref:
                raise PilotError("runtime_secret_alias_required")
            report = asyncio.run(
                probe(
                    config,
                    manifest,
                    api_key=key,
                    check_structured=args.check_structured,
                    check_tools=args.check_tools,
                    ca_file=args.ca_file,
                )
            )
        else:
            if args.runtime_output or args.check_tools or args.check_structured:
                raise PilotError("invalid_benchmark_options")
            report = asyncio.run(
                benchmark(
                    config,
                    manifest,
                    api_key=key,
                    requests=args.requests,
                    hourly_price=args.hourly_price,
                    billable_seconds=args.billable_seconds,
                    currency=args.currency,
                    ca_file=args.ca_file,
                )
            )
        if args.runtime_output:
            write_json(
                args.runtime_output, build_runtime_payload(report, args.secret_ref, args.owner_id)
            )
        if args.output:
            write_json(args.output, report)
        else:
            print(json.dumps(report, allow_nan=False, indent=2))
        return 0
    except Exception as error:
        code = error.code if isinstance(error, PilotError) else "invalid_pilot_input"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
