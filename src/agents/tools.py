from .tool_registry import ToolRegistry
import httpx

@ToolRegistry.register(description="Searches the internal knowledge base for relevant documents.")
async def search_knowledge_base(query: str, limit: int = 5) -> list[dict[str, str]]:
    """
    Асинхронный вызов pgvector или внешнего API.
    Pydantic автоматически проверит, что query - это str, а limit - это int.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post("http://vector-db/search", json={"q": query, "limit": limit})
        response.raise_for_status()
        return response.json()