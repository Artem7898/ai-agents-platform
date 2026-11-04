"""Per-attempt admission and settlement for the provider transport.

Only workflow calls with an InvocationContext enter this module. The SDK clone
shares its owner's HTTP pool, and must never be closed independently. Reservations
estimate input tokens from UTF-8 bytes; they are not a tokenizer guarantee.
"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Awaitable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from src.usage.contracts import InvocationContext, Reservation

T = TypeVar("T")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


class ProviderTransportError(ValueError):
    """A safe error without vendor bodies, headers, prompts or credentials."""

    def __init__(self, code: str, accounted_tokens: int, unknown_calls: int):
        super().__init__(f"Provider request failed ({code})")
        self.code = code
        self.accounted_tokens = accounted_tokens
        self.unknown_calls = unknown_calls


@dataclass(frozen=True)
class MeteredCompletion:
    response: Any
    accounted_tokens: int
    unknown_calls: int


def normalized_usage(response: Any) -> dict[str, int | None] | None:
    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    usage = {}
    for field in TOKEN_FIELDS:
        value = raw.get(field) if isinstance(raw, dict) else getattr(raw, field, None)
        usage[field] = value if type(value) is int and 0 <= value < 2**63 else None
    prompt, completion, total = (usage[field] for field in TOKEN_FIELDS)
    if total is not None and total < (prompt or 0) + (completion or 0):
        usage["total_tokens"] = None
    return usage


def _setting(name: str, default: Any) -> Any:
    from django.conf import settings

    return getattr(settings, name, default) if settings.configured else default


def estimate_tokens(parameters: dict[str, Any], max_output_tokens: int) -> int:
    # Do not persist this serialization: messages and tool schemas contain data.
    inputs = {
        key: parameters[key]
        for key in ("messages", "tools", "response_format")
        if key in parameters
    }
    input_bytes = len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    overhead = max(0, int(_setting("LLM_RESERVATION_OVERHEAD_TOKENS", 512)))
    return input_bytes + overhead + max_output_tokens


def _request_id(response: Any, credential: str | None = None) -> str | None:
    value = getattr(response, "_request_id", None) or getattr(response, "request_id", None)
    if isinstance(value, str) and credential and credential in value:
        return None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value):
        return value
    return None


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, (APIConnectionError, httpx.NetworkError)):
        return "connection_error"
    if isinstance(exc, APIStatusError):
        return f"http_{exc.status_code}"
    return "invalid_response" if isinstance(exc, ValueError) else "transport_error"


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, (APIConnectionError, httpx.TimeoutException, httpx.NetworkError)) or (
        isinstance(exc, APIStatusError)
        and (exc.status_code in (408, 409, 429) or exc.status_code >= 500)
    )


async def _join_shielded(task: asyncio.Future[T]) -> tuple[T, asyncio.CancelledError | None]:
    cancelled = None
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            # Repeated worker cancellation cannot cancel the financial task.
            cancelled = exc


async def _shield(operation: Awaitable[T]) -> T:
    result, cancelled = await _join_shielded(asyncio.ensure_future(operation))
    if cancelled is not None:
        raise cancelled
    return result


async def reserve(context: InvocationContext, **kwargs: Any) -> Reservation:
    # Lazy import keeps legacy/direct LLMService calls independent of the ORM.
    from src.usage import services

    return await services.reserve(context, **kwargs)


async def _reserve_attempt(context: InvocationContext, **kwargs: Any) -> Reservation:
    # The transaction may commit while its async caller is being cancelled.
    # Retrieve its result and release the unsent reservation before propagating.
    reservation, cancelled = await _join_shielded(asyncio.create_task(reserve(context, **kwargs)))
    if cancelled is not None:
        await _settle(reservation, context, status="cancelled", error=cancelled)
        raise cancelled
    return reservation


async def mark_dispatched(invocation_id: UUID, execution_token: UUID | None) -> None:
    from src.usage import services

    await services.mark_dispatched(invocation_id, execution_token=execution_token)


async def finalize(invocation_id: UUID, **kwargs: Any) -> None:
    from src.usage import services

    await services.finalize(invocation_id, **kwargs)


async def _settle(
    reservation: Reservation,
    context: InvocationContext,
    *,
    status: str,
    usage: dict[str, int | None] | None = None,
    response: Any = None,
    error: BaseException | None = None,
    credential: str | None = None,
) -> None:
    await _shield(
        finalize(
            reservation.id,
            status=status,
            usage=usage,
            provider_request_id=_request_id(
                response if response is not None else error, credential
            ),
            error_code=_error_code(error) if error is not None else None,
            execution_token=context.execution_token,
        )
    )


def _budget(reservation: Reservation, usage: dict[str, int | None] | None) -> tuple[int, int]:
    total = usage.get("total_tokens") if usage else None
    return (total, 0) if total is not None else (reservation.reserved_tokens, 1)


async def _retry_delay(attempt: int) -> None:
    await asyncio.sleep(max(0.0, float(_setting("LLM_RETRY_BASE_SECONDS", 0.25))) * (2**attempt))


async def metered_completion(
    client: AsyncOpenAI,
    parameters: dict[str, Any],
    context: InvocationContext,
    *,
    runtime_id: UUID | None,
    provider: str,
    max_output_tokens: int,
    operation: str,
) -> MeteredCompletion:
    client = client.with_options(max_retries=0)
    retries = min(2, max(0, int(_setting("LLM_TRANSPORT_MAX_RETRIES", 2))))
    accounted = unknown = 0
    for attempt in range(retries + 1):
        reservation = await _reserve_attempt(
            context,
            model_name=parameters["model"],
            runtime_id=runtime_id,
            provider=provider,
            estimated_tokens=estimate_tokens(parameters, max_output_tokens),
            max_output_tokens=max_output_tokens,
            operation=operation,
        )
        dispatched = False
        try:
            await mark_dispatched(reservation.id, context.execution_token)
            dispatched = True
            response = await client.chat.completions.create(**parameters)
        except BaseException as exc:
            await _settle(
                reservation,
                context,
                status="cancelled"
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else "failed",
                error=exc,
                credential=client.api_key,
            )
            if dispatched:
                accounted += reservation.reserved_tokens
                unknown += 1
            if dispatched and attempt < retries and _retryable(exc):
                await _retry_delay(attempt)
                continue
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) or not dispatched:
                raise
            raise ProviderTransportError(_error_code(exc), accounted, unknown) from None
        usage = normalized_usage(response)
        await _settle(
            reservation,
            context,
            status="succeeded",
            usage=usage,
            response=response,
            credential=client.api_key,
        )
        cost, missing = _budget(reservation, usage)
        return MeteredCompletion(response, accounted + cost, unknown + missing)
    raise AssertionError("Unreachable provider retry state")


async def metered_stream(
    client: AsyncOpenAI,
    parameters: dict[str, Any],
    context: InvocationContext,
    *,
    runtime_id: UUID | None,
    provider: str,
    max_output_tokens: int,
) -> AsyncGenerator[Any, None]:
    client = client.with_options(max_retries=0)
    retries = min(2, max(0, int(_setting("LLM_TRANSPORT_MAX_RETRIES", 2))))
    accounted = unknown_calls = 0
    for attempt in range(retries + 1):
        reservation = await _reserve_attempt(
            context,
            model_name=parameters["model"],
            runtime_id=runtime_id,
            provider=provider,
            estimated_tokens=estimate_tokens(parameters, max_output_tokens),
            max_output_tokens=max_output_tokens,
            operation="stream",
        )
        dispatched = saw_chunk = False
        stream = None
        usage = None
        response = None
        try:
            await mark_dispatched(reservation.id, context.execution_token)
            dispatched = True
            stream = await client.chat.completions.create(**parameters)
            async for chunk in stream:
                saw_chunk = True
                candidate = normalized_usage(chunk)
                if candidate is not None:
                    usage = candidate
                response = chunk
                yield chunk
            await _shield(stream.close())
        except BaseException as exc:
            try:
                if stream is not None:
                    try:
                        await _shield(stream.close())
                    except Exception:
                        # Preserve the original safe error and still settle.
                        pass
            finally:
                await _settle(
                    reservation,
                    context,
                    status="cancelled"
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    else "failed",
                    usage=usage,
                    response=response,
                    error=exc,
                    credential=client.api_key,
                )
            if dispatched:
                cost, missing = _budget(reservation, usage)
                accounted += cost
                unknown_calls += missing
            if dispatched and not saw_chunk and attempt < retries and _retryable(exc):
                await _retry_delay(attempt)
                continue
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) or not dispatched:
                raise
            raise ProviderTransportError(_error_code(exc), accounted, unknown_calls) from None
        else:
            await _settle(
                reservation,
                context,
                status="succeeded",
                usage=usage,
                response=response,
                credential=client.api_key,
            )
            return
