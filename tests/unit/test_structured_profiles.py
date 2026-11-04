"""Structured results use each runtime's declared OpenAI-compatible features."""

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI
from pydantic import SecretStr

import src.runtimes.clients as clients
from src.agents.llm_service import LLMService, close_llm_client
from src.agents.schemas import AgentSpec
from src.runtimes.schemas import RuntimeCapabilities, RuntimeSpec
from src.runtimes.secrets import RuntimeConfigurationError


@pytest_asyncio.fixture(autouse=True)
async def close_clients():
    yield
    await close_llm_client()


@pytest.fixture
def provider(monkeypatch, settings):
    requests = []
    settings.LLM_ALLOWED_BASE_URLS = [
        "https://api.openai.com/v1",
        "https://cloud.example/v1",
        "http://localhost:11434/v1",
    ]
    settings.LLM_RUNTIME_SECRETS = {"structured-cloud": SecretStr("test-only-key")}

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-structured",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"approved":true,"score":0.9}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            },
        )

    def connect(base_url, credential):
        return AsyncOpenAI(
            base_url=base_url,
            api_key=credential.value.get_secret_value(),
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    monkeypatch.setattr(clients, "_create_client", connect)
    return requests


def runtime_for(profile, *, supported=True):
    endpoint = {
        "openai": "https://api.openai.com/v1",
        "openai_compatible": "https://cloud.example/v1",
        "ollama": "http://localhost:11434/v1",
    }[profile]
    return RuntimeSpec(
        id=str(uuid.uuid4()),
        name=profile,
        profile=profile,
        base_url=endpoint,
        secret_ref=None if profile == "ollama" else "structured-cloud",
        allowed_models=("model",),
        capabilities=RuntimeCapabilities(
            structured_outputs=supported,
            token_limit_parameter="max_completion_tokens" if profile == "openai" else "max_tokens",
        ),
        revision=1,
    )


@pytest.mark.parametrize("profile", ["openai", "openai_compatible", "ollama"])
async def test_structured_result_honors_runtime_request_contract(provider, profile):
    from src.workflows.output_schemas import OutputSchemaRef, OutputSchemaRegistry

    runtime = runtime_for(profile)
    agent = AgentSpec(
        name="Reviewer", model_name="model", system_prompt="Review", runtime_id=runtime.id
    )
    target = OutputSchemaRegistry.get(OutputSchemaRef(id="review_decision", version=1)).model
    response = await LLMService().generate_structured_response(
        agent, "Review this", target, runtime=runtime, max_output_tokens=25
    )
    assert response.data == {"approved": True, "score": 0.9}
    assert response.usage.total_tokens == 10
    assert len(provider) == 1
    request = json.loads(provider[0].content)
    token_field = "max_completion_tokens" if profile == "openai" else "max_tokens"
    other_field = "max_tokens" if profile == "openai" else "max_completion_tokens"
    assert request[token_field] == 25 and other_field not in request
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {"approved", "score"}
    assert "tools" not in request


async def test_runtime_without_structured_support_rejects_before_provider_http(provider):
    from src.workflows.output_schemas import OutputSchemaRef, OutputSchemaRegistry

    runtime = runtime_for("ollama", supported=False)
    agent = AgentSpec(
        name="Reviewer", model_name="model", system_prompt="Review", runtime_id=runtime.id
    )
    target = OutputSchemaRegistry.get(OutputSchemaRef(id="review_decision", version=1)).model
    with pytest.raises(RuntimeConfigurationError, match="structured_outputs"):
        await LLMService().generate_structured_response(agent, "Review", target, runtime=runtime)
    assert provider == []
