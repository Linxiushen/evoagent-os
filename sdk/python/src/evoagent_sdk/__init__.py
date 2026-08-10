"""Typed Python client for EvoAgent OS."""

from .client import Client
from .errors import ApiError, EvoAgentError, ProtocolError

__version__ = "0.1.0"

__all__ = ["ApiError", "Client", "EvoAgentError", "ProtocolError", "__version__"]
