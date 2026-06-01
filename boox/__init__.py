#  SPDX-License-Identifier: MIT
"""Boox cloud client — public surface. Body in client.py / _http.py / auth.py / errors.py / pushread.py (split in #45)."""

# Top-level so tests can patch via ``boox.requests`` / ``boox.oss2``.
import oss2
import requests

from boox._version import __version__
from boox.client import BooxClient, read_config
from boox.errors import (
    APIError, AuthError, BooxError, NotFoundError, OSSError, RateLimitError,
)

Boox = BooxClient  # legacy alias

__all__ = [
    "APIError", "AuthError", "Boox", "BooxClient", "BooxError", "NotFoundError",
    "OSSError", "RateLimitError", "read_config", "__version__",
]
