"""HTTP client for the cloud sync API. Outbound only: the office opens every connection."""

from __future__ import annotations

from typing import Any

import httpx

from klinik_shared.sync_protocol import (
    MAX_PULL_LIMIT,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    ChangesResponse,
    ClientCodesResponse,
    PushRequest,
    PushResponse,
)


class CloudError(Exception):
    """Base class; ``retryable`` tells the worker whether to try again later."""

    retryable = True


class CloudUnavailable(CloudError):
    """Network failure or timeout: the cloud (or the office internet) is down."""


class CloudServerError(CloudError):
    """The cloud answered with a 5xx."""


class CloudAuthFailed(CloudError):
    """The sync token was refused. Retrying will not help; a person must fix the token."""

    retryable = False


class ProtocolMismatch(CloudError):
    """The cloud speaks another protocol version. One side needs an update."""

    retryable = False


class CloudClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 40.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                PROTOCOL_HEADER: str(PROTOCOL_VERSION),
                "User-Agent": "klinik-sync",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudUnavailable(type(exc).__name__) from exc
        if response.status_code == 401:
            raise CloudAuthFailed("token refused")
        if response.status_code == 426:
            raise ProtocolMismatch("protocol version")
        if response.status_code >= 500:
            raise CloudServerError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # A 4xx other than auth means this worker sent something malformed.
            raise CloudServerError(f"HTTP {response.status_code}")
        return response.json()

    async def ping(self) -> None:
        await self._request("GET", "/sync/v1/ping")

    async def changes(
        self, after: int, *, wait: int = 0, limit: int = MAX_PULL_LIMIT
    ) -> ChangesResponse:
        data = await self._request(
            "GET", "/sync/v1/changes", params={"after": after, "limit": limit, "wait": wait}
        )
        return ChangesResponse.model_validate(data)

    async def push(self, request: PushRequest) -> PushResponse:
        data = await self._request(
            "POST", "/sync/v1/items", content=request.model_dump_json(), headers={
                "Content-Type": "application/json"
            }
        )  # fmt: skip
        return PushResponse.model_validate(data)

    async def client_codes(self) -> ClientCodesResponse:
        return ClientCodesResponse.model_validate(
            await self._request("GET", "/sync/v1/client-codes")
        )
