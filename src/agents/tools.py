"""Built-in adapters. Registration happens once from AppConfig.ready()."""

from typing import Annotated, Any

import httpx
from django.conf import settings
from pydantic import Field

from .tool_registry import ToolRegistry, current_tool_idempotency_key


@ToolRegistry.register(description="Searches the configured internal knowledge base.")
async def search_knowledge_base(
    query: Annotated[str, Field(min_length=1, max_length=10_000)],
    limit: Annotated[int, Field(ge=1, le=50)] = 5,
) -> list[dict[str, Any]]:
    endpoint = getattr(settings, "KNOWLEDGE_BASE_SEARCH_URL", "")
    if not endpoint:
        raise ValueError("KNOWLEDGE_BASE_SEARCH_URL is not configured")
    key = current_tool_idempotency_key()
    headers = {"Idempotency-Key": key} if key else {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(endpoint, json={"q": query, "limit": limit}, headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("Knowledge base returned an invalid result")
    return data
