"""Mock HTTP verifies profile selection, isolation and capability enforcement."""

import asyncio
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI
from pydantic import BaseModel, SecretStr

import src.runtimes.clients as clients
from src.agents.llm_service import LLMResponseError, LLMService, close_llm_client
from src.agents.schemas import AgentSpec, ToolSpec
from src.runtimes.schemas import RuntimeCapabilities, RuntimeSpec
from src.runtimes.secrets import RuntimeConfigurationError


def make_runtime(profile="openai", **changes):
    values = {
        "id": str(uuid.uuid4()),
        "name": profile,
        "profile": profile,
        "base_url": "https://api.openai.com/v1",
        "secret_ref": "cloud",
        "allowed_models": ("model",),
        "capabilities": RuntimeCapabilities(
            tools=True,
            structured_outputs=True,
            streaming=True,
            usage=True,
            strict_tools=True,
            temperature=True,
            token_limit_parameter="max_completion_tokens",
        ),
        "revision": 1,
    }
    values.update(changes)
    return RuntimeSpec(**values)


def agent_for(runtime):
    return AgentSpec(
        name="Agent", model_name="model", system_prompt="Assist", runtime_id=runtime.id
    )


def completion(content="Answer", **changes):
    result = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 123,
        "model": "model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    result.update(changes)
    return result


@pytest.fixture
def transport(monkeypatch, settings):
    requests = []
    settings.LLM_ALLOWED_BASE_URLS = [
        "https://api.openai.com/v1",
        "https://cloud.example/v1",
        "http://localhost:11434/v1",
    ]
    settings.LLM_RUNTIME_SECRETS = {"cloud": SecretStr("test-cloud-key")}

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    handlers = [handler]

    def create(base_url, credential):
        return AsyncOpenAI(
            base_url=base_url,
            api_key=credential.value.get_secret_value(),
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handlers[0]), follow_redirects=False
            ),
        )

    monkeypatch.setattr(clients, "_create_client", create)
    return requests, handlers


@pytest_asyncio.fixture(autouse=True)
async def close_clients():
    yield
    await close_llm_client()


async def test_concurrent_cloud_and_ollama_keep_credentials_and_payloads_isolated(transport):
    requests, handlers = transport
    both_arrived = asyncio.Event()

    async def handler(request):
        requests.append(request)
        if len(requests) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=2)
        return httpx.Response(200, json=completion())

    handlers[0] = handler
    cloud = make_runtime()
    local = make_runtime(
        "ollama",
        base_url="http://localhost:11434/v1",
        secret_ref=None,
        capabilities=RuntimeCapabilities(token_limit_parameter="max_tokens"),
    )
    service = LLMService()
    results = await asyncio.gather(
        service.generate_with_tools(agent_for(cloud), "Cloud question", runtime=cloud),
        service.generate_with_tools(agent_for(local), "Local question", runtime=local),
    )
    assert all(result.usage.total_tokens == 10 for result in results)
    by_host = {request.url.host: request for request in requests}
    remote, local_request = by_host["api.openai.com"], by_host["localhost"]
    assert remote.headers["authorization"] == "Bearer test-cloud-key"
    assert local_request.headers["authorization"] == "Bearer ollama-local-no-auth"
    cloud_body, local_body = json.loads(remote.content), json.loads(local_request.content)
    assert cloud_body["messages"][-1]["content"] == "Cloud question"
    assert local_body["messages"][-1]["content"] == "Local question"
    assert cloud_body["max_completion_tokens"] == 1024
    assert "max_tokens" not in cloud_body
    assert local_body["max_tokens"] == 1024
    assert "max_completion_tokens" not in local_body
    assert "temperature" not in local_body


async def test_compatible_profile_sends_regular_tools_and_declared_token_parameter(transport):
    requests, _ = transport
    spec = make_runtime(
        "openai_compatible",
        base_url="https://cloud.example/v1",
        capabilities=RuntimeCapabilities(tools=True, token_limit_parameter="max_tokens"),
    )
    tool = ToolSpec(
        name="weather",
        description="Weather",
        parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}},
    )
    await LLMService().generate_with_tools(agent_for(spec), "Question", [tool], runtime=spec)
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 1024
    assert "max_completion_tokens" not in body
    function = body["tools"][0]["function"]
    assert "strict" not in function
    assert "additionalProperties" not in function["parameters"]


@pytest.mark.parametrize("mode", ["tools", "structured", "stream"])
async def test_unsupported_capability_fails_before_http(transport, mode):
    requests, _ = transport
    spec = make_runtime(capabilities=RuntimeCapabilities())
    service = LLMService()

    class Result(BaseModel):
        value: int

    with pytest.raises(RuntimeConfigurationError, match="support"):
        if mode == "tools":
            await service.generate_with_tools(
                agent_for(spec),
                "Question",
                [ToolSpec(name="weather", description="Weather", parameters_schema={})],
                runtime=spec,
            )
        elif mode == "structured":
            await service.generate_structured(agent_for(spec), "Question", Result, runtime=spec)
        else:
            await anext(service.stream_raw(agent_for(spec), "Question", runtime=spec))
    assert not requests


@pytest.mark.parametrize("change", ["missing", "mismatch", "model", "disabled"])
async def test_assignment_and_model_policy_fail_before_http(transport, change):
    requests, _ = transport
    spec = make_runtime()
    agent = agent_for(spec)
    if change == "missing":
        spec = None
    elif change == "mismatch":
        spec = spec.model_copy(update={"id": str(uuid.uuid4())})
    elif change == "model":
        agent = agent.model_copy(update={"model_name": "unapproved"})
    else:
        spec = spec.model_copy(update={"enabled": False})
    with pytest.raises(RuntimeConfigurationError):
        await LLMService().generate_with_tools(agent, "Question", runtime=spec)
    assert not requests


async def test_runtime_provider_error_does_not_leak_echoed_credentials(transport):
    requests, handlers = transport

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            401,
            json={"error": {"message": "echo test-cloud-key", "type": "authentication_error"}},
        )

    handlers[0] = handler
    spec = make_runtime()
    with pytest.raises(LLMResponseError, match="HTTP 401") as error:
        await LLMService().generate_with_tools(agent_for(spec), "Question", runtime=spec)
    assert "test-cloud-key" not in str(error.value)
    assert error.value.__suppress_context__


async def test_runtime_structured_validation_error_does_not_echo_provider_data(transport):
    _, handlers = transport

    class Result(BaseModel):
        value: int

    async def handler(request):
        return httpx.Response(200, json=completion('{"value":"test-cloud-key"}'))

    handlers[0] = handler
    spec = make_runtime()
    with pytest.raises(LLMResponseError, match="invalid structured result") as error:
        await LLMService().generate_structured(agent_for(spec), "Question", Result, runtime=spec)
    assert "test-cloud-key" not in str(error.value)


async def test_provider_redirect_is_not_followed(transport):
    requests, handlers = transport

    async def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://unapproved.example/steal"})

    handlers[0] = handler
    spec = make_runtime()
    with pytest.raises(LLMResponseError, match="HTTP 307"):
        await LLMService().generate_with_tools(agent_for(spec), "Question", runtime=spec)
    assert len(requests) == 1
    assert requests[0].url.host == "api.openai.com"


async def test_runtime_structured_output_sends_declared_schema_and_validates_result(transport):
    requests, handlers = transport

    class Result(BaseModel):
        value: int

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion('{"value":7}'))

    handlers[0] = handler
    runtime = make_runtime()
    result = await LLMService().generate_structured(
        agent_for(runtime), "Score", Result, runtime=runtime
    )
    assert result.value == 7
    body = json.loads(requests[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 1024


@pytest.mark.parametrize("include_usage", [False, True])
async def test_ollama_stream_uses_profile_and_requests_usage_only_when_declared(
    transport, include_usage
):
    requests, handlers = transport
    chunks = [
        {
            "id": "chunk",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hello"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chunk",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    content = (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    )

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    handlers[0] = handler
    runtime = make_runtime(
        "ollama",
        base_url="http://localhost:11434/v1",
        secret_ref=None,
        capabilities=RuntimeCapabilities(streaming=True, usage=include_usage),
    )
    output = [
        chunk async for chunk in LLMService().stream_raw(agent_for(runtime), "Hi", runtime=runtime)
    ]
    assert output == ["Hello"]
    body = json.loads(requests[0].content)
    assert body["stream"] is True
    assert body["max_tokens"] == 1024
    if include_usage:
        assert body["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in body
