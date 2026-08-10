from __future__ import annotations

from typing import Any


class EvoAgentError(RuntimeError):
    """Base exception for SDK failures."""


class ApiError(EvoAgentError):
    """A non-success response returned by the control plane."""

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        url: str,
        detail: str,
        response_body: Any = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.detail = detail
        self.response_body = response_body
        super().__init__(f"{method} {url} returned {status_code}: {detail}")


class ProtocolError(EvoAgentError):
    """A successful response did not match the published contract."""
