"""Sync Gateway protocol primitives — ``/neocloud/*`` replication wire.

Phase 4 #34 implements the five Couchbase Sync Gateway primitives the
web UI's PouchDB client uses to replicate against ``/neocloud/``:
``_changes``, ``_bulk_get``, ``_bulk_docs``, ``_revs_diff``, and
``_local``. Higher-level "sync this channel" wrappers (NOTE_TREE,
READER_LIBRARY) build on these in #36 / #37; the local SQLite mirror
lands in #35; binary fetch in #38; conflict resolution in #40.

Auth boundary
-------------

``/neocloud/*`` uses the **SyncGatewaySession cookie**, NOT the Bearer
JWT. The cookie is minted at startup by Phase 1 #27's
:meth:`boox.auth.Auth.mint_sync_session` and exposed as
``client.sync_token``. Every primitive here reads ``client.sync_token``
and sends ``Cookie: SyncGatewaySession=<value>`` with no
``Authorization`` header — that's why this module talks to
``requests`` directly rather than going through ``BooxClient.api_call``
(which always attaches the Bearer header). If ``client.sync_token``
is None, :class:`boox.errors.AuthError` is raised at the primitive
boundary with a message pointing at ``mint_sync_session()``.

Pattern A wiring
----------------

Per ``flora/boox-plan-2026-05-31.md`` §Decisions #6, this module
attaches to ``BooxClient`` as ``self.sync = SyncClient(self)`` and
methods surface as ``client.sync.changes(...)``,
``client.sync.bulk_get(...)``, etc. Matches the convention already
followed by ``pushread``, ``subscriptions``, ``files``, and
``screensavers``.

HAR coverage
------------

Every wire shape here was confirmed against ``tools/boox/captures/``
(entry-count audit per ``memory/feedback_dispatch_har_pre_confirmed_check.md``).
Hit counts per capture as of 2026-06-01:

- ``_changes``     — 128 reqs across 9 HARs (longpoll variant in 5).
- ``_bulk_get``    — 2 reqs (both 406 → multipart fallback exercised).
- ``_bulk_docs``   — 14 reqs across 6 HARs.
- ``_revs_diff``   — 16 reqs across 6 HARs.
- ``_local``       — 338 reqs across 10 HARs (GET + PUT).

Judgement calls (also surfaced in PR body, per standing rail #4)
----------------------------------------------------------------

1. **``_bulk_get`` 406 is the only observed response**: every captured
   ``_bulk_get`` in HAR returned 406 with ``{"error":"Not Acceptable",
   "reason":"Response is multipart"}``. The server appears to always
   prefer multipart over JSON for this endpoint. We still attempt the
   ``application/json`` POST first (matching PouchDB's behavior — it
   asks for JSON, gracefully degrades) and fall back to per-doc GETs
   with ``open_revs=[<rev>]``. If a future capture shows a 200 JSON
   response we'll already handle it; until then, the fallback is the
   hot path.

2. **``local_get`` returns ``None`` on 404**: HAR shows 404 with body
   ``{"error":"not_found","reason":"missing"}`` for fresh checkpoint
   keys (replication has never run before). Replication-checkpoint
   semantics expect a "no checkpoint yet" sentinel, so returning
   ``None`` is more useful than raising :class:`NotFoundError`.
   Callers that genuinely need to distinguish "no checkpoint" from
   "transient 404" can re-check via :meth:`local_put`.

3. **``changes`` returns a ``ChangesResult``** with both ``.results``
   (iterable of change records) and ``.last_seq`` (the opaque
   sequence token to persist for the next poll). Splitting these into
   a single return type avoids the two-call dance of a bare
   generator + a separate "give me last_seq" method, and keeps the
   API obvious: ``r = client.sync.changes(ch); for c in r: ... ;
   checkpoint = r.last_seq``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

import requests

from boox.errors import (
    APIError,
    AuthError,
    BooxError,
    NotFoundError,
    from_response as _error_from_response,
)


__all__ = ["ChangesResult", "SyncClient", "SyncProtocolError"]


class SyncProtocolError(APIError):
    """Sync-protocol-shaped failure that isn't a generic ``APIError``.

    Reserved for cases where the Sync Gateway response is structurally
    wrong for the primitive we sent (e.g., ``_revs_diff`` echoing back
    a non-dict body). Distinct from :class:`APIError` so callers that
    want to retry transport-level failures can leave protocol-level
    failures to bubble up.
    """


class ChangesResult:
    """Iterable result envelope for :meth:`SyncClient.changes`.

    Iterating yields change records from the ``results`` array; the
    ``last_seq`` attribute carries the opaque sequence token the
    caller should persist for the next ``since=`` poll.

    Sync Gateway sequence tokens look like ``"1364795318::1364813623"``
    — treat as opaque, don't parse them client-side.
    """

    __slots__ = ("_results", "last_seq")

    def __init__(self, results: Sequence[Mapping[str, Any]], last_seq: Any) -> None:
        self._results = list(results)
        self.last_seq = last_seq

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self._results)

    def __len__(self) -> int:
        return len(self._results)


def _cookie_missing_error() -> AuthError:
    return AuthError(
        "SyncGatewaySession cookie not set on client.sync_token; call "
        "boox.auth.Auth.mint_sync_session() (or set BOOX_SYNC_TOKEN in "
        "config) before invoking /neocloud/* primitives."
    )


class SyncClient:
    """Couchbase Sync Gateway primitives attached to a ``BooxClient``.

    All methods route through :meth:`_request`, which enforces the
    SyncGatewaySession cookie and refuses to fire if it's unset.
    """

    def __init__(self, client) -> None:
        self._c = client

    # --------------------------- transport --------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
        allow_404: bool = False,
    ) -> requests.Response:
        """Fire a ``/neocloud/<path>`` request with cookie auth.

        Returns the raw ``requests.Response`` so callers can inspect
        status / headers (notably for the 406 fallback path on
        ``_bulk_get``). Maps non-2xx to typed exceptions via
        :func:`boox.errors.from_response` *unless* ``allow_404=True``
        (used by ``local_get`` to convert 404 into ``None``).

        Raises :class:`AuthError` if ``client.sync_token`` is unset —
        before we ever hit the network.
        """
        client = self._c
        if not client.sync_token:
            raise _cookie_missing_error()

        url = f"https://{client.cloud}/neocloud{path}"
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Cookie": f"SyncGatewaySession={client.sync_token}",
        }
        body: Optional[str] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, separators=(",", ":"))

        r = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body,
            timeout=timeout,
        )

        if allow_404 and r.status_code == 404:
            return r
        # 406 is handled explicitly by bulk_get; let it through here so
        # the caller can branch without first catching an exception.
        if r.status_code == 406:
            return r

        exc = _error_from_response(r)
        if exc is not None:
            raise exc

        return r

    # --------------------------- _changes ---------------------------------

    def changes(
        self,
        channel: str,
        since: Optional[str] = None,
        limit: int = 1000,
        longpoll: bool = False,
        heartbeat_ms: int = 30000,
        timeout: Optional[float] = None,
    ) -> ChangesResult:
        """Poll ``/neocloud/_changes`` for a channel.

        Wire (HAR-confirmed, all 9 ``/neocloud/*`` HARs)::

            GET /neocloud/_changes?style=all_docs
                                  &filter=sync_gateway/bychannel
                                  &channels=<channel>
                                  &since=<seq>
                                  &limit=<n>
                                  [&feed=longpoll&heartbeat=<ms>]

        Response shape::

            {"results": [{...change record...}, ...], "last_seq": "<seq>"}

        Returns a :class:`ChangesResult` whose iteration yields the
        change records and whose ``.last_seq`` is the opaque sequence
        token to persist for the next poll. ``since=None`` means "from
        the beginning of the channel" (the server interprets a missing
        ``since`` as zero).

        Longpoll behavior: when ``longpoll=True``, the server hangs the
        request open for up to ``heartbeat_ms`` waiting for changes
        rather than returning an empty ``results`` immediately. The
        transport ``timeout`` defaults to a generous value above the
        heartbeat so a heartbeat tick doesn't fire a client-side
        timeout; pass ``timeout`` explicitly if you need a tighter
        bound. Long-running connections aren't free — callers that
        poll in a hot loop should consider non-longpoll instead.
        """
        params: Dict[str, Any] = {
            "style": "all_docs",
            "filter": "sync_gateway/bychannel",
            "channels": channel,
            "limit": limit,
        }
        if since is not None:
            params["since"] = since
        if longpoll:
            params["feed"] = "longpoll"
            params["heartbeat"] = heartbeat_ms
            # Allow the server's heartbeat plus a 5-second slack before
            # we give up. Caller can override.
            if timeout is None:
                timeout = (heartbeat_ms / 1000.0) + 5.0

        r = self._request("GET", "/_changes", params=params, timeout=timeout)
        body = r.json()
        results = body.get("results", [])
        last_seq = body.get("last_seq")
        return ChangesResult(results=results, last_seq=last_seq)

    # --------------------------- _bulk_get --------------------------------

    def bulk_get(
        self,
        doc_revs: Sequence[Mapping[str, str]],
    ) -> List[Mapping[str, Any]]:
        """Fetch full doc bodies for ``(id, rev)`` pairs.

        Wire (HAR-confirmed, ``notes-har-2026-05-31.har`` entry
        bulk_get + open_revs fallback)::

            POST /neocloud/_bulk_get?revs=true&latest=true
            { "docs": [ {"id": "...", "rev": "..."}, ... ] }

        Sync Gateway prefers multipart for this endpoint. Every
        captured ``_bulk_get`` we have returns ``406 Not Acceptable``
        with body ``{"error":"Not Acceptable","reason":"Response is
        multipart"}`` when we ask for JSON. The fallback is per-doc::

            GET /neocloud/<doc_id>?revs=true&latest=true&open_revs=["<rev>"]

        Each fallback GET returns a JSON array ``[{"ok": {...doc...}}]``
        (or ``[{"missing": "<rev>"}]`` if the rev is gone). This method
        flattens those into a single list of result entries — preserving
        the per-doc ``ok`` / ``missing`` shape so callers can detect
        gaps rather than silently dropping them.

        Returns a list of per-rev result envelopes. For each input
        ``(id, rev)`` you'll get one entry in the output:
        ``{"ok": <doc body>}`` on success or
        ``{"missing": "<rev>", "id": "<id>"}`` if the server doesn't
        have that revision.
        """
        if not doc_revs:
            return []

        params = {"revs": "true", "latest": "true"}
        body = {"docs": [dict(d) for d in doc_revs]}

        r = self._request(
            "POST", "/_bulk_get", params=params, json_body=body
        )

        if r.status_code == 406:
            return self._bulk_get_fallback(doc_revs)

        # Success path: PouchDB-style ``{"results": [{"id": ..., "docs":
        # [{"ok": {...}}]}]}``. Flatten ``docs`` across results.
        parsed = r.json()
        out: List[Mapping[str, Any]] = []
        for entry in parsed.get("results", []):
            doc_id = entry.get("id")
            for d in entry.get("docs", []):
                if "missing" in d and "id" not in d:
                    d = dict(d)
                    d["id"] = doc_id
                out.append(d)
        return out

    def _bulk_get_fallback(
        self,
        doc_revs: Sequence[Mapping[str, str]],
    ) -> List[Mapping[str, Any]]:
        """Per-doc ``GET ?open_revs=[...]`` fallback for 406 responses.

        Fires one GET per ``(id, rev)`` pair. Each response is a JSON
        array — flatten into the caller's return list, preserving
        ``ok`` / ``missing`` entries as the bulk-success path would.
        """
        out: List[Mapping[str, Any]] = []
        for dr in doc_revs:
            doc_id = dr["id"]
            rev = dr["rev"]
            params = {
                "revs": "true",
                "latest": "true",
                "open_revs": json.dumps([rev]),
            }
            r = self._request("GET", f"/{doc_id}", params=params)
            entries = r.json()
            for entry in entries:
                if "missing" in entry and "id" not in entry:
                    entry = dict(entry)
                    entry["id"] = doc_id
                out.append(entry)
        return out

    # --------------------------- _bulk_docs -------------------------------

    def bulk_docs(
        self,
        docs: Sequence[Mapping[str, Any]],
        new_edits: bool = False,
    ) -> List[Mapping[str, Any]]:
        """Push docs up to the server in one request.

        Wire (HAR-confirmed, multiple captures)::

            POST /neocloud/_bulk_docs
            { "docs": [...], "new_edits": false }

        ``new_edits=False`` is the replication-push convention — the
        server stores the rev exactly as given rather than generating
        a new one. PouchDB always uses this for push-replication, and
        :mod:`boox.files` / :mod:`boox.screensavers` use it for their
        MESSAGE-channel registrations (see :meth:`BooxClient._push_message_doc`,
        :meth:`ScreensaversClient._register_screensaver_doc`).

        Response is a JSON array of ``{"id": "...", "rev": "..."}``
        entries — one per input doc. Errors for individual docs come
        back as ``{"id": "...", "error": "...", "reason": "..."}``
        in the same array (server returns 201 even on partial success).
        """
        body = {"docs": [dict(d) for d in docs], "new_edits": new_edits}
        r = self._request("POST", "/_bulk_docs", json_body=body)
        return r.json()

    # --------------------------- _revs_diff -------------------------------

    def revs_diff(
        self,
        missing: Mapping[str, Sequence[str]],
    ) -> Mapping[str, Mapping[str, Any]]:
        """Ask the server which of these ``(doc_id, [revs])`` it lacks.

        Wire (HAR-confirmed)::

            POST /neocloud/_revs_diff
            { "<doc_id>": ["<rev>", ...], ... }

        Response::

            { "<doc_id>": {"missing": ["<rev>", ...],
                           "possible_ancestors": ["<rev>", ...]}, ... }

        Only doc_ids the server is missing at least one rev for appear
        in the response. ``possible_ancestors`` is a hint the server
        gives so the client can do efficient ``_bulk_get`` calls
        building on already-stored revs.
        """
        body = {k: list(v) for k, v in missing.items()}
        r = self._request("POST", "/_revs_diff", json_body=body)
        parsed = r.json()
        if not isinstance(parsed, dict):
            raise SyncProtocolError(
                f"_revs_diff returned non-dict body: {type(parsed).__name__}",
                response_body=r.text,
                status_code=r.status_code,
            )
        return parsed

    # --------------------------- _local -----------------------------------

    def local_get(self, key: str) -> Optional[Mapping[str, Any]]:
        """GET a replication checkpoint by key.

        Wire (HAR-confirmed)::

            GET /neocloud/_local/<key>

        Returns the parsed JSON doc on success, or ``None`` if the key
        doesn't exist (404 with ``{"error":"not_found","reason":"missing"}``).
        Replication checkpoints are write-then-read: a 404 here means
        "no checkpoint yet, start from scratch", not an error condition.
        """
        r = self._request("GET", f"/_local/{key}", allow_404=True)
        if r.status_code == 404:
            return None
        return r.json()

    def local_put(
        self,
        key: str,
        doc: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """PUT a replication checkpoint by key.

        Wire (HAR-confirmed)::

            PUT /neocloud/_local/<key>
            { "_id": "_local/<key>", "_rev": "<rev>", ...checkpoint... }

        Response is ``{"id": "_local/<key>", "ok": true, "rev": "<new_rev>"}``
        with status 201. The caller is responsible for assembling the
        checkpoint body (PouchDB convention: ``history`` list of
        ``{last_seq, session_id}`` entries, top-level ``last_seq``
        and ``session_id``).
        """
        r = self._request("PUT", f"/_local/{key}", json_body=doc)
        return r.json()
