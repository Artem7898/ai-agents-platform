"""The execution API uses the same Django token identity as DRF."""

from typing import Annotated, Any

from asgiref.sync import sync_to_async
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

authorization = APIKeyHeader(name="Authorization", auto_error=False)


async def require_user(value: Annotated[str | None, Depends(authorization)]) -> Any:
    """Authenticate a DRF token without accepting ambient browser cookies."""
    parts = value.split() if value else []
    if len(parts) != 2 or parts[0].lower() != "token":
        raise HTTPException(
            status_code=401,
            detail="Use Authorization: Token <key>",
            headers={"WWW-Authenticate": "Token"},
        )
    try:
        user, _ = await sync_to_async(
            TokenAuthentication().authenticate_credentials, thread_sensitive=True
        )(parts[1])
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or inactive token",
            headers={"WWW-Authenticate": "Token"},
        ) from exc
    return user
