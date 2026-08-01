from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection

import httpx


def require_content_type(
    response: httpx.Response,
    allowed: Collection[str],
    *,
    source: str,
) -> str:
    """Require an explicitly allow-listed response media type."""

    raw_content_type = response.headers.get("content-type")
    if raw_content_type is None:
        raise RuntimeError(f"{source} omitted the Content-Type header")
    media_type = raw_content_type.partition(";")[0].strip().lower()
    normalized_allowed = {item.lower() for item in allowed}
    if media_type not in normalized_allowed:
        raise RuntimeError(f"{source} returned an unsupported Content-Type")

    content_encoding = response.headers.get("content-encoding", "identity")
    if content_encoding.strip().lower() != "identity":
        raise RuntimeError(f"{source} returned an unsupported Content-Encoding")
    return media_type


def validated_content_length(
    response: httpx.Response,
    max_bytes: int,
    *,
    source: str,
) -> int | None:
    """Validate Content-Length before a response body is consumed."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    if response.headers.get("transfer-encoding"):
        raise RuntimeError(f"{source} returned conflicting framing headers")
    stripped = raw_length.strip()
    if not stripped.isascii() or not stripped.isdigit():
        raise RuntimeError(f"{source} returned an invalid Content-Length")
    announced = int(stripped)
    if announced > max_bytes:
        raise RuntimeError(f"{source} announced an oversized response")
    return announced


async def iter_bounded_raw(
    response: httpx.Response,
    max_bytes: int,
    *,
    source: str,
) -> AsyncIterator[bytes]:
    """Yield raw chunks while enforcing announced and observed byte limits."""

    announced = validated_content_length(response, max_bytes, source=source)
    received = 0
    async for chunk in response.aiter_raw():
        if not chunk:
            continue
        next_total = received + len(chunk)
        if next_total > max_bytes:
            raise RuntimeError(f"{source} exceeded the response size limit")
        received = next_total
        yield chunk
    if announced is not None and received != announced:
        raise RuntimeError(f"{source} did not match its Content-Length")


async def read_bounded_body(
    response: httpx.Response,
    max_bytes: int,
    *,
    source: str,
) -> bytes:
    """Read a small response without ever accepting more than ``max_bytes``."""

    body = bytearray()
    async for chunk in iter_bounded_raw(
        response,
        max_bytes,
        source=source,
    ):
        body.extend(chunk)
    return bytes(body)


async def iter_bounded_lines(
    response: httpx.Response,
    *,
    max_line_bytes: int,
    max_total_bytes: int,
    source: str,
) -> AsyncIterator[bytes]:
    """Incrementally split raw response chunks into strictly bounded lines."""

    if max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be positive")
    pending = bytearray()
    async for chunk in iter_bounded_raw(
        response,
        max_total_bytes,
        source=source,
    ):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline
            part_length = end - start
            if len(pending) + part_length > max_line_bytes:
                raise RuntimeError(f"{source} returned an oversized line")
            if part_length:
                pending.extend(memoryview(chunk)[start:end])
            if newline < 0:
                break
            if pending.endswith(b"\r"):
                pending.pop()
            yield bytes(pending)
            pending.clear()
            start = newline + 1
    if pending:
        yield bytes(pending)


class ManagedAsyncClient:
    """A lazy, loop-safe HTTP connection pool with explicit ownership."""

    def __init__(
        self,
        timeout: httpx.Timeout | float,
        *,
        max_connections: int = 8,
        max_keepalive_connections: int = 4,
        keepalive_expiry: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = False,
    ) -> None:
        self._timeout = timeout
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self._transport = transport
        self._trust_env = trust_env
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def get(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("HTTP adapter has already been closed")
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._closed:
                raise RuntimeError("HTTP adapter has already been closed")
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=self._timeout,
                    limits=self._limits,
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=self._trust_env,
                )
            return self._client

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()
