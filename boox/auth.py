"""JWT loading + inspection, and SyncGatewaySession runtime mint.

Two responsibilities live here:

1. **JWT helpers** (module-level pure functions):

   - ``load_token`` — resolve the Bearer JWT from explicit arg, env, config
     mapping, or a ``KEY=VALUE`` env file.
   - ``decode_jwt`` — parse the JWT payload (no signature verification —
     Boox's HMAC secret isn't ours; we just inspect ``iat`` / ``exp`` /
     ``id`` / ``loginType``).
   - ``is_expired`` — boolean check against current wall time with leeway.
   - ``time_to_expiry`` — remaining seconds until ``exp``.

2. **Auth subobject** for ``BooxClient`` (Pattern A — see
   ``flora/boox-plan-2026-05-31.md`` §Decisions #6):

   - ``Auth(client).mint_sync_session()`` — ``GET /api/1/users/syncToken``
     to runtime-derive the SyncGatewaySession cookie from the Bearer JWT.
     Used in ``BooxClient.__init__`` so the cookie no longer needs to be
     separately harvested into ``secrets/boox.env``; the Bearer JWT is the
     sole manually-refreshed credential.

JWT refresh procedure (manual, captcha-gated — Phase 5 will look at
automation)
---------------------------------------------------------------------

The Bearer JWT issued by Boox is good for roughly ten weeks (current token
expires 2026-08-17). When it expires:

1. Open ``https://push.boox.com`` in a clean browser tab.
2. Log in with phone or email — solve the Aliyun slider captcha when prompted.
3. Open devtools → Network. Click any request to ``/api/1/*``.
4. Copy the ``Authorization: Bearer <JWT>`` header value (just the JWT
   portion, after ``Bearer ``).
5. Paste it into ``config.ini`` under ``[default] token = ...`` or into the
   workspace ``secrets/boox.env`` as ``BOOX_TOKEN=...``.

There is no usable direct API path for refresh: ``users/sendVerifyCode`` is
captcha-gated (web UI submits an Aliyun slider-solve token in the ``verify``
field; result_code 100011 — labeled ``phoneRegisterNotSupported`` — is
returned for either an unsupported phone or an unsolved captcha, so the
error is misleading). See ``flora/BOOX.md`` §"Token storage" for the wider
context.

**Important 2026-05-31 change:** the SyncGatewaySession cookie (used for
``/neocloud/*`` calls) is **no longer a separately-harvested credential**.
``mint_sync_session`` issues it at runtime from the Bearer JWT via
``GET /api/1/users/syncToken``. Keep the ``BOOX_SYNC_TOKEN`` field around
as a fallback (used only if the mint call fails), but new setups don't
need to populate it.
"""

import base64
import binascii
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from boox.errors import APIError, AuthError


# --------------------------- JWT helpers ----------------------------------


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment, padding to a multiple of 4."""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def decode_jwt(token: str) -> dict:
    """Parse the JWT payload and return it as a dict.

    No signature verification — Boox's HMAC secret isn't ours, and we only
    need the claims (``id``, ``loginType``, ``iat``, ``exp``) for expiry
    bookkeeping.

    Raises ``AuthError`` if the token isn't a well-formed three-segment
    JWT or the payload isn't valid base64url-encoded JSON.
    """
    if not isinstance(token, str) or not token:
        raise AuthError("empty or non-string token")
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError(
            f"malformed JWT: expected 3 segments, got {len(parts)}"
        )
    try:
        payload_bytes = _b64url_decode(parts[1])
    except (binascii.Error, ValueError) as exc:
        raise AuthError(f"malformed JWT payload (base64): {exc}") from exc
    try:
        return json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise AuthError(f"malformed JWT payload (json): {exc}") from exc


def time_to_expiry(token: str) -> int:
    """Return seconds remaining until ``exp`` (negative if already expired).

    Raises ``AuthError`` if the token lacks an ``exp`` claim (Boox issues
    all tokens with one, so absence indicates a malformed token).
    """
    payload = decode_jwt(token)
    exp = payload.get("exp")
    if exp is None:
        raise AuthError("JWT payload missing 'exp' claim")
    return int(exp - time.time())


def is_expired(token: str, leeway_seconds: int = 60) -> bool:
    """Return True if ``token`` is within ``leeway_seconds`` of ``exp``.

    Default 60s of leeway so a token about to expire is treated as
    already expired — avoids racing the cloud's clock for borderline
    refreshes.
    """
    return time_to_expiry(token) <= leeway_seconds


# --------------------------- Token loading --------------------------------


def _parse_env_file(path: Path, var_name: str) -> Optional[str]:
    """Return the value of ``var_name`` from a simple ``KEY=VALUE`` env file.

    Tolerates leading ``export`` and single- or double-quoted values, the
    same shape ``tests/conftest.py`` accepts for live-test secrets.
    Returns None if the file is absent or the var isn't present.
    """
    if not path.is_file():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line.startswith(var_name):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


def load_token(
    config: Optional[Mapping[str, Any]] = None,
    env_var: str = "BOOX_TOKEN",
    env_file: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a Bearer JWT from one of the supported sources.

    Priority order (first hit wins):

    1. The ``env_var`` env variable (default ``BOOX_TOKEN``).
    2. The ``config`` mapping's ``[default][token]`` slot (matches the
       shape ``configparser`` exposes for ``config.ini``).
    3. The ``BOOX_SECRETS_FILE`` env var, if set, pointed at an env-style
       file containing a ``<env_var>=...`` line.
    4. The explicitly-passed ``env_file`` path (a ``secrets/boox.env``-style
       file).

    Returns the token string, or None if no source has it.
    """
    value = os.environ.get(env_var)
    if value:
        return value

    if config is not None:
        try:
            token = config["default"]["token"]
        except (KeyError, TypeError):
            token = None
        if token:
            return token

    explicit_secrets_file = os.environ.get("BOOX_SECRETS_FILE")
    if explicit_secrets_file:
        token = _parse_env_file(Path(explicit_secrets_file), env_var)
        if token:
            return token

    if env_file is not None:
        token = _parse_env_file(Path(env_file), env_var)
        if token:
            return token

    return None


# --------------------------- Auth subobject -------------------------------


class Auth:
    """Pattern A subobject — wired as ``self.auth = Auth(self)`` on
    ``BooxClient``. Holds a back-reference to the client for HTTP + token
    access; methods compose against ``self._c.api_call(...)``.
    """

    def __init__(self, client):
        self._c = client

    def mint_sync_session(self) -> dict:
        """Mint a SyncGatewaySession cookie from the Bearer JWT.

        Issues ``GET /api/1/users/syncToken`` and returns the parsed
        ``data`` block — ``{session_id, expires, cookie_name, channels}``
        (HAR-confirmed from settings HAR 2026-05-31).

        Side effect: assigns ``self._c.sync_token = data['session_id']`` so
        downstream ``/neocloud/*`` callers (notably ``send_file``'s
        ``_push_message_doc``) pick up the freshly-minted cookie without
        any further plumbing.

        Raises ``AuthError`` if the cloud returns a non-zero ``result_code``
        or the response shape doesn't carry ``session_id``. Network
        exceptions from ``requests`` propagate unchanged so callers can
        distinguish transport failures (retry candidate) from auth
        failures (not a retry candidate).

        The non-zero ``result_code`` path is now driven by ``api_call``'s
        typed-error mapping (#28): ``api_call`` raises ``APIError`` which
        we re-raise as ``AuthError`` to preserve the long-standing
        contract that the init chain's ``except (AuthError, ...)`` catches
        a failed syncToken mint.
        """
        try:
            resp = self._c.api_call("users/syncToken")
        except APIError as exc:
            raise AuthError(
                f"users/syncToken failed: {exc}",
                response_body=exc.response_body,
                status_code=exc.status_code,
                result_code=exc.result_code,
            ) from exc
        data = resp.get("data") or {}
        session_id = data.get("session_id")
        if not session_id:
            raise AuthError(
                f"users/syncToken response missing session_id: {resp}"
            )
        self._c.sync_token = session_id
        logging.debug(
            "minted SyncGatewaySession; expires=%s channels=%s",
            data.get("expires"),
            data.get("channels"),
        )
        return data


__all__ = [
    "Auth",
    "AuthError",
    "decode_jwt",
    "is_expired",
    "load_token",
    "time_to_expiry",
]
