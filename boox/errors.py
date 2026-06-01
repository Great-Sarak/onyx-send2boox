"""Typed exception hierarchy for Boox client failures.

Before #28 the client returned whatever JSON the cloud sent back, including
error envelopes with ``result_code != 0`` or HTTP 4xx/5xx. Callers had to
inspect the response themselves, which is easy to forget. This module
introduces typed exceptions so failures surface clearly at the call site and
tests can assert specific failure modes.

Class hierarchy
---------------

``BooxError`` is the base. ``api_call`` maps HTTP status + ``result_code``
to the most specific subclass it can:

- ``AuthError`` — HTTP 401 (auth required / token rejected). The existing
  ``boox.auth.AuthError`` is re-exported as an alias of this class so
  ``from boox.auth import AuthError`` keeps working, and ``isinstance``
  checks against either name succeed.
- ``RateLimitError`` — HTTP 429. We haven't observed Boox using 429 in any
  HAR (see #28 ScopeNote in PR body); the class exists for forward
  compatibility so callers can register a handler now.
- ``NotFoundError`` — HTTP 404.
- ``APIError`` — generic API failure: non-zero ``result_code`` on a 2xx
  response, HTTP 5xx (Boox 5xxs carry the same ``result_code`` envelope —
  see ``rsses/url/content`` in the rss-subscribe HAR), or any other 4xx
  that doesn't have a dedicated subclass.
- ``OSSError`` — wraps ``oss2.exceptions.OssError`` (and subclasses:
  ``AccessDenied``, ``RequestError`` etc.) raised during ``send_file``'s
  ``oss2.resumable_upload`` call. The original oss2 exception is
  accessible via ``__cause__`` (``raise OSSError(...) from oss_exc``)
  and via the ``oss_exception`` property for clarity.

Exception state
---------------

Every ``BooxError`` raised from an HTTP response carries:

- ``response_body`` — the raw response text (string) if the body was
  readable, else None.
- ``status_code`` — the HTTP status int, or None if the error didn't
  originate from an HTTP response (e.g., wrapped oss2 exception).
- ``result_code`` — the ``result_code`` int from the JSON body, or None
  if the body wasn't JSON / didn't have one.

``response_body`` deliberately stores the raw text rather than the parsed
dict so a caller debugging a real failure can see exactly what the server
sent — Boox occasionally returns non-JSON HTML error pages from upstream
proxies, and silently swallowing those during error mapping would hide
the actual diagnostic.

Network failures (decision documented on issue #28)
---------------------------------------------------

``requests.exceptions.ConnectionError`` (and other ``RequestException``
subclasses) **propagate unchanged** from ``api_call``. Rationale:

1. Network failures are transport-layer concerns, not API failures.
   They're a retry candidate; API-level errors generally are not.
2. The existing codebase already catches ``requests.RequestException``
   at the relevant sites (``boox/__init__.py``'s init chain, the
   ``Auth.mint_sync_session`` docstring) — wrapping would force every
   caller to learn a second exception type with the same semantics.
3. ``BooxError`` is for *Boox-API*-shaped failures (something the cloud
   said). A failed TCP connect tells us nothing about what the cloud
   would have said.

OSS errors are wrapped because oss2 lives below our abstraction layer
and we don't want callers to import oss2 just to catch upload failures.

Known result_code values
------------------------

Catalog of what we've observed in HARs and JS bundles. Future
contributors should extend this list as more are observed; the goal is
that an unfamiliar code in a bug report can be looked up here.

- ``0`` (``RESULT_CODE_SUCCESS``) — operation succeeded.
- ``1`` (``RESULT_CODE_GENERIC_FAILURE``) — generic server-side failure.
  Seen in HARs on HTTP 500 responses from ``rsses/url/content`` and
  ``rsses/opml/import`` (the upstream feed-fetcher choked).
- ``100011`` (``RESULT_CODE_INVALID_INPUT``) — labelled
  ``phoneRegisterNotSupported`` in the JS bundle but actually returned
  for any "invalid input" / "captcha needed" condition during
  ``users/sendVerifyCode``. See ``boox/auth.py`` "JWT refresh procedure".
"""

from __future__ import annotations

import json
from typing import Any, Optional


# --------------------------- result_code constants ------------------------

#: ``result_code`` value indicating the API call succeeded.
RESULT_CODE_SUCCESS = 0

#: Generic non-success ``result_code`` — Boox uses this for most server-side
#: failures, including HTTP 500s from the feed-fetcher endpoints.
RESULT_CODE_GENERIC_FAILURE = 1

#: ``result_code`` returned by ``users/sendVerifyCode`` when the input is
#: invalid or the captcha hasn't been solved. Labelled
#: ``phoneRegisterNotSupported`` in the JS bundle but used as a generic
#: "invalid input" indicator.
RESULT_CODE_INVALID_INPUT = 100011


# --------------------------- exception hierarchy --------------------------


class BooxError(Exception):
    """Base for all Boox client errors.

    Carries optional response metadata so callers can inspect what the
    server said without re-running the call. All fields default to None
    because not every error originates from an HTTP response (notably
    ``OSSError`` wraps oss2 failures which don't have a Boox response).
    """

    def __init__(
        self,
        message: str = "",
        *,
        response_body: Optional[str] = None,
        status_code: Optional[int] = None,
        result_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.response_body = response_body
        self.status_code = status_code
        self.result_code = result_code


class AuthError(BooxError):
    """HTTP 401 — auth required or token rejected.

    Also raised by ``boox.auth`` for token loading / decoding / minting
    failures that aren't HTTP-response-shaped; those instances will have
    ``status_code`` / ``response_body`` / ``result_code`` of None.
    """


class RateLimitError(BooxError):
    """HTTP 429 — caller hit a rate limit.

    Boox hasn't been observed using 429 in any of our HAR captures; this
    class exists so callers can register a handler now and have it
    automatically activate if the server starts emitting 429s.
    """


class NotFoundError(BooxError):
    """HTTP 404 — requested resource doesn't exist."""


class APIError(BooxError):
    """Generic API-level failure.

    Covers three cases the dedicated subclasses don't:

    - HTTP 2xx with non-zero ``result_code`` (the application-level
      "request shape was fine, server refused" path — this is what most
      Boox failures look like).
    - HTTP 5xx (Boox 5xxs typically come with a ``result_code: 1``
      envelope; we still raise ``APIError`` rather than a dedicated
      ``ServerError`` because the caller's recovery is the same).
    - Other 4xx without a dedicated subclass (e.g., 403, 422).
    """


class OSSError(BooxError):
    """Wraps an ``oss2.exceptions.OssError`` raised during an upload.

    The original oss2 exception is the ``__cause__`` (set by
    ``raise OSSError(...) from oss_exc``). Use the ``oss_exception``
    property for a clearer accessor at call sites that need to inspect
    the wrapped exception's request-id / OSS error code.
    """

    @property
    def oss_exception(self) -> Optional[BaseException]:
        """Return the wrapped oss2 exception, if any."""
        return self.__cause__


# --------------------------- response → exception mapping ----------------


def _parse_body(response_text: Optional[str]) -> Optional[dict]:
    """Return the parsed JSON body, or None if it's missing / not JSON.

    Boox usually responds with JSON even on errors, but upstream proxies
    sometimes return HTML — keep parsing tolerant so the typed-error
    layer doesn't ALSO crash trying to extract a message.
    """
    if not response_text:
        return None
    try:
        return json.loads(response_text)
    except (ValueError, TypeError):
        return None


def _format_message(
    status_code: int,
    result_code: Optional[int],
    server_message: Optional[str],
) -> str:
    """Build a human-readable message string for a raised exception.

    Format keeps the most identifying fields up front: HTTP status, then
    ``result_code``, then the server's textual message. A debugger
    skimming a traceback should see what went wrong without unpacking
    the exception attributes.
    """
    parts = [f"HTTP {status_code}"]
    if result_code is not None:
        parts.append(f"result_code={result_code}")
    if server_message:
        parts.append(f"message={server_message!r}")
    return " ".join(parts)


def from_response(response: Any) -> Optional[BooxError]:
    """Map a ``requests.Response`` to the appropriate ``BooxError``.

    Returns the exception instance for the caller to raise (so the
    raise statement appears in the caller's traceback). Returns None
    if the response indicates success — caller should treat None as
    "no error, proceed".

    Mapping rules:

    - 2xx + ``result_code == 0`` (or no result_code field) → None.
    - 2xx + non-zero ``result_code`` → ``APIError``.
    - 401 → ``AuthError``.
    - 404 → ``NotFoundError``.
    - 429 → ``RateLimitError``.
    - 5xx → ``APIError`` (Boox 5xxs carry result_code envelopes too).
    - Other 4xx → ``APIError``.

    The exception is populated with ``status_code``, ``result_code``
    (parsed from the JSON body if present), and ``response_body`` (raw
    text, for debug inspection).
    """
    status_code = response.status_code
    body_text = response.text if hasattr(response, "text") else None
    body = _parse_body(body_text)

    result_code = None
    server_message = None
    if isinstance(body, dict):
        result_code = body.get("result_code")
        server_message = body.get("message")

    # Success path: 2xx and either result_code is 0 or absent.
    if 200 <= status_code < 300:
        if result_code in (None, RESULT_CODE_SUCCESS):
            return None
        message = _format_message(status_code, result_code, server_message)
        return APIError(
            message,
            response_body=body_text,
            status_code=status_code,
            result_code=result_code,
        )

    # Status-coded failures: pick the most specific class we have.
    if status_code == 401:
        cls = AuthError
    elif status_code == 404:
        cls = NotFoundError
    elif status_code == 429:
        cls = RateLimitError
    else:
        cls = APIError

    message = _format_message(status_code, result_code, server_message)
    return cls(
        message,
        response_body=body_text,
        status_code=status_code,
        result_code=result_code,
    )


__all__ = [
    "APIError",
    "AuthError",
    "BooxError",
    "NotFoundError",
    "OSSError",
    "RateLimitError",
    "RESULT_CODE_GENERIC_FAILURE",
    "RESULT_CODE_INVALID_INPUT",
    "RESULT_CODE_SUCCESS",
    "from_response",
]
