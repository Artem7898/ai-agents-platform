"""Actual SDK HTTP attempts are admitted and settled without a database."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

import src.agents.llm_service as llm_module
import src.usage.transport as transport
from src.agents.llm_service import LLMResponseError, LLMService
from src.agents.schemas import AgentSpec
from src.usage.contracts import InvocationContext, QuotaExceeded, Reservation

SECRET = "sk-provider-secret-never-record"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def completion(*, usage=USAGE, content="Answer", finish_reason="stop"):
    result = {
        "id": "chat-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage is not None:
        result["usage"] = usage
    return result


@pytest.fixture
def agent():
    return AgentSpec(name="Metered", model_name="test", system_prompt="Private instructions")


@pytest.fixture
def context():
    return InvocationContext(uuid4(), "node", 0, uuid4())


@pytest.fixture
def ledger(monkeypatch, settings):
    settings.LLM_RETRY_BASE_SECONDS = 0
    settings.LLM_TRANSPORT_MAX_RETRIES = 2
    records = []

    async def reserve(ctx, **kwargs):
        record = SimpleNamespace(
            id=uuid4(),
            arguments=kwargs,
            context=ctx,
            dispatched=False,
            final=None,
        )
        records.append(record)
        return Reservation(record.id, kwargs["estimated_tokens"], len(records))

    async def dispatched(identifier, execution_token):
        record = next(row for row in records if row.id == identifier)
        assert execution_token == record.context.execution_token
        record.dispatched = True

    async def finalize(identifier, **kwargs):
        await asyncio.sleep(0)
        record = next(row for row in records if row.id == identifier)
        assert record.final is None, "One financial settlement per physical attempt"
        record.final = kwargs

    monkeypatch.setattr(transport, "reserve", reserve)
    monkeypatch.setattr(transport, "mark_dispatched", dispatched)
    monkeypatch.setattr(transport, "finalize", finalize)
    return records


@pytest.fixture
async def provider(monkeypatch):
    clients = []

    def setup(handler):
        client = AsyncOpenAI(
            api_key=SECRET,
            base_url="https://provider.example/v1",
            max_retries=2,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        monkeypatch.setattr(llm_module, "get_llm_client", lambda: client)
        return client

    yield setup
    for client in clients:
        await client.close()


async def test_explicit_retries_have_one_reservation_per_http_attempt(
    agent,
    context,
    ledger,
    provider,
):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(500, json={"error": {"message": SECRET}})
        return httpx.Response(200, json=completion(), headers={"x-request-id": "req-safe_1"})

    client = provider(handler)
    result = await LLMService().generate_with_tools(agent, "Question", invocation_context=context)
    assert len(requests) == len(ledger) == 3
    assert client.max_retries == 2  # The shared original was not mutated or closed.
    assert not client.is_closed()
    assert [row.final["status"] for row in ledger] == ["failed", "failed", "succeeded"]
    assert [row.final["error_code"] for row in ledger] == ["http_500", "http_500", None]
    assert ledger[-1].final["provider_request_id"] == "req-safe_1"
    assert (
        result.accounted_tokens == sum(row.arguments["estimated_tokens"] for row in ledger[:2]) + 18
    )
    assert result.usage.total_tokens == 18
    assert result.unknown_calls == 2
    stored = repr([vars(row) for row in ledger])
    assert SECRET not in stored
    assert "Private instructions" not in stored
    assert all(row.dispatched for row in ledger)


@pytest.mark.parametrize("status, attempts", [(401, 1), (403, 1), (400, 1), (429, 3), (503, 3)])
async def test_failure_retries_are_bounded_and_errors_safe(
    agent,
    context,
    ledger,
    provider,
    status,
    attempts,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": SECRET, "code": SECRET}},
            headers={"x-request-id": "unsafe\tidentifier"},
        )

    provider(handler)
    with pytest.raises(transport.ProviderTransportError) as raised:
        await LLMService().generate_with_tools(agent, invocation_context=context)
    assert len(calls) == len(ledger) == attempts
    assert raised.value.unknown_calls == attempts
    assert SECRET not in str(raised.value)
    assert all(row.final["provider_request_id"] is None for row in ledger)
    assert all(row.final["error_code"] == f"http_{status}" for row in ledger)


async def test_quota_denial_never_touches_provider(agent, context, ledger, provider, monkeypatch):
    calls = []
    provider(lambda request: calls.append(request))

    async def denied(*_args, **_kwargs):
        raise QuotaExceeded(uuid4())

    monkeypatch.setattr(transport, "reserve", denied)
    with pytest.raises(QuotaExceeded):
        await LLMService().generate_with_tools(agent, invocation_context=context)
    assert calls == ledger == []


async def test_timeout_has_no_hidden_sdk_retries(agent, context, ledger, provider):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    provider(handler)
    with pytest.raises(transport.ProviderTransportError, match="timeout"):
        await LLMService().generate_with_tools(agent, invocation_context=context)
    assert len(calls) == len(ledger) == 3
    assert all(row.final["usage"] is None for row in ledger)
    assert all(row.final["error_code"] == "timeout" for row in ledger)


@pytest.mark.parametrize(
    "usage, total, missing",
    [
        (None, None, True),
        ({"prompt_tokens": 5}, None, True),
        ({"total_tokens": 7}, 7, False),
        (dict.fromkeys(USAGE, 0), 0, False),
        ({"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 2}, None, True),
    ],
)
async def test_missing_partial_and_zero_usage_are_distinct(
    agent,
    context,
    ledger,
    provider,
    usage,
    total,
    missing,
):
    provider(lambda _: httpx.Response(200, json=completion(usage=usage)))
    result = await LLMService().generate_with_tools(agent, invocation_context=context)
    assert result.usage.total_tokens == total
    assert result.unknown_calls == int(missing)
    assert result.accounted_tokens == (
        ledger[0].arguments["estimated_tokens"] if missing else total
    )
    if usage is None:
        assert result.usage.prompt_tokens is None
        assert ledger[0].final["usage"] is None
    elif "prompt_tokens" in usage:
        assert result.usage.prompt_tokens == usage["prompt_tokens"]


@pytest.mark.parametrize("content, finish_reason", [("{invalid", "stop"), ("{}", "length")])
async def test_bad_semantic_response_still_settles_reported_usage(
    agent,
    context,
    ledger,
    provider,
    content,
    finish_reason,
):
    class Score(BaseModel):
        score: int

    provider(
        lambda _: httpx.Response(
            200,
            json=completion(content=content, finish_reason=finish_reason),
        )
    )
    with pytest.raises((ValidationError, LLMResponseError)):
        await LLMService().generate_structured(
            agent,
            "Score",
            Score,
            invocation_context=context,
        )
    assert len(ledger) == 1
    assert ledger[0].final["usage"] == USAGE


async def test_cancellation_after_dispatch_finishes_financial_cleanup(
    agent,
    context,
    ledger,
    provider,
):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    provider(handler)
    task = asyncio.create_task(LLMService().generate_with_tools(agent, invocation_context=context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ledger) == 1
    assert ledger[0].dispatched
    assert ledger[0].final["status"] == "cancelled"
    assert ledger[0].final["usage"] is None


async def test_cancellation_before_dispatch_is_settled_as_unsent(
    agent,
    context,
    ledger,
    provider,
    monkeypatch,
):
    entered = asyncio.Event()
    calls = []
    provider(lambda request: calls.append(request))

    async def pending(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "mark_dispatched", pending)
    task = asyncio.create_task(LLMService().generate_with_tools(agent, invocation_context=context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
    assert not ledger[0].dispatched
    assert ledger[0].final["status"] == "cancelled"


async def test_cancellation_while_reserving_releases_committed_unsent_call(
    agent,
    context,
    ledger,
    provider,
    monkeypatch,
):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = transport.reserve

    async def delayed(*args, **kwargs):
        reservation = await original(*args, **kwargs)
        entered.set()
        await finish.wait()
        return reservation

    monkeypatch.setattr(transport, "reserve", delayed)
    calls = []
    provider(lambda request: calls.append(request))
    task = asyncio.create_task(LLMService().generate_with_tools(agent, invocation_context=context))
    await entered.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []
    assert not ledger[0].dispatched
    assert ledger[0].final["status"] == "cancelled"


async def test_cancellation_during_success_settlement_does_not_lose_reported_usage(
    agent,
    context,
    ledger,
    provider,
    monkeypatch,
):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = transport.finalize

    async def delayed(*args, **kwargs):
        entered.set()
        await finish.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(transport, "finalize", delayed)
    provider(lambda _: httpx.Response(200, json=completion()))
    task = asyncio.create_task(LLMService().generate_with_tools(agent, invocation_context=context))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # A second shutdown signal must also leave cleanup shielded.
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ledger) == 1
    assert ledger[0].final["usage"] == USAGE


def sse(*, usage=True):
    chunks = [
        {
            "id": "chunk-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
        }
    ]
    if usage:
        chunks.append(
            {
                "id": "chunk-1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [],
                "usage": USAGE,
            }
        )
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


async def test_stream_consumes_empty_choice_usage_chunk(agent, context, ledger, provider):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=sse(), headers={"content-type": "text/event-stream"})

    provider(handler)
    assert [
        text
        async for text in LLMService().stream_raw(
            agent,
            "Question",
            invocation_context=context,
        )
    ] == ["hello"]
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert len(ledger) == 1
    assert ledger[0].final["status"] == "succeeded"
    assert ledger[0].final["usage"] == USAGE


async def test_early_stream_close_keeps_usage_unknown(agent, context, ledger, provider):
    provider(
        lambda _: httpx.Response(
            200,
            text=sse(),
            headers={"content-type": "text/event-stream"},
        )
    )
    stream = LLMService().stream_raw(agent, "Question", invocation_context=context)
    assert await anext(stream) == "hello"
    await stream.aclose()
    assert len(ledger) == 1
    assert ledger[0].final["status"] == "cancelled"
    assert ledger[0].final["usage"] is None


async def test_completed_stream_without_usage_stays_unknown(agent, context, ledger, provider):
    provider(
        lambda _: httpx.Response(
            200,
            text=sse(usage=False),
            headers={"content-type": "text/event-stream"},
        )
    )
    assert [
        text
        async for text in LLMService().stream_raw(
            agent,
            "Question",
            invocation_context=context,
        )
    ] == ["hello"]
    assert ledger[0].final["status"] == "succeeded"
    assert ledger[0].final["usage"] is None


@pytest.mark.parametrize("before_chunk", [True, False])
async def test_stream_retries_only_before_first_provider_chunk(
    agent,
    context,
    ledger,
    provider,
    before_chunk,
):
    calls = []
    closed = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if not before_chunk:
                yield sse(usage=False).split("data: [DONE]")[0].encode()
            raise httpx.ReadError(SECRET)

        async def aclose(self):
            closed.append(True)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                stream=FailingStream(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, text=sse(), headers={"content-type": "text/event-stream"})

    provider(handler)
    stream = LLMService().stream_raw(agent, "Question", invocation_context=context)
    if before_chunk:
        assert [text async for text in stream] == ["hello"]
        assert len(calls) == len(ledger) == 2
        assert ledger[1].final["usage"] == USAGE
    else:
        assert await anext(stream) == "hello"
        with pytest.raises(transport.ProviderTransportError) as raised:
            await anext(stream)
        assert SECRET not in str(raised.value)
        assert len(calls) == len(ledger) == 1
    assert closed
    assert ledger[0].final["status"] == "failed"
    assert ledger[0].final["usage"] is None


def test_estimate_includes_utf8_schema_and_output_without_persisting_them(settings):
    settings.LLM_RESERVATION_OVERHEAD_TOKENS = 512
    plain = {"model": "test", "messages": [{"role": "user", "content": "Привет"}]}
    with_schema = {
        **plain,
        "response_format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert transport.estimate_tokens(with_schema, 20) > transport.estimate_tokens(plain, 20)
    assert transport.estimate_tokens(plain, 20) > len("Привет".encode()) + 512 + 20


def test_invalid_usage_fields_are_never_coerced_or_fabricated():
    result = transport.normalized_usage(
        SimpleNamespace(
            usage={"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "20"},
        )
    )
    assert result == dict.fromkeys(USAGE, None)


def test_token_counters_fit_the_ledger_bigint_domain():
    assert transport.normalized_usage(SimpleNamespace(usage={"total_tokens": 2**63})) == (
        dict.fromkeys(USAGE, None)
    )


async def test_provider_request_id_cannot_echo_its_credential(agent, context, ledger, provider):
    provider(
        lambda _: httpx.Response(
            200,
            json=completion(),
            headers={"x-request-id": f"req-{SECRET}"},
        )
    )
    await LLMService().generate_with_tools(agent, invocation_context=context)
    assert ledger[0].final["provider_request_id"] is None
    assert SECRET not in repr(vars(ledger[0]))
