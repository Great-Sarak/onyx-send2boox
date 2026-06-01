"""Subscriptions module — catalog ops, folders, list, recommend, detail, preview, OPML, unsubscribe.

Pattern A subobject (project decision #6, locked 2026-05-31): wired as
``self.subscriptions = SubscriptionsClient(self)`` on ``BooxClient``. Methods
compose against ``self._c.api_call(...)``.

Endpoint coverage (all HAR-confirmed):

- ``GET  /api/1/subscribe/list``         — push-boox-har-2026-05-31.json, rss-subscribe-har-2026-05-31.json.
- ``POST /api/1/subscribe/folder``       — push-boox-har-2026-05-31.json.
- ``POST /api/1/subscribe/sub``          — push-boox-har-2026-05-31.json, rss-subscribe-har-2026-05-31.json.
- ``GET  /api/1/rsses/public/search``    — push-boox-har-2026-05-31.json.
- ``GET  /api/1/rsses/public/recommend`` — push-boox-har-2026-05-31.json.
- ``GET  /api/1/rsses/one/detail``       — push-boox-har-2026-05-31.json, rss-subscribe-har-2026-05-31.json.
- ``POST /api/1/rsses/url/content``      — push-boox-har-2026-05-31.json, rss-subscribe-har-2026-05-31.json.
- ``POST /api/1/webpage/bat/del``        — rss-subscribe-har-2026-05-31.json (#31).
- ``POST /api/1/rsses/opml/export``      — push-boox-har-2026-05-31.json, rss-subscribe-har-2026-05-31.json (#31).
- ``GET  /uploads/feed/export/<uuid>.opml`` — same HARs, follow-up fetch (#31).
- ``POST /api/1/rsses/opml/import``      — rss-subscribe-har-2026-05-31.json (#31; observed 500 in capture).

Catalog-only limitation: the web UI subscribe flow only succeeds for feeds
already in Boox's curated public catalog. ``search_catalog`` returns
``count: 0`` for URLs not in the catalog (HAR-confirmed against
``https://www.atlassian.com/blog/rss``, ``https://culpaeus.peacock-walleye.ts.net:8312/opds``,
and several others). The OPML-import workaround for custom URLs lives on
``import_opml`` below — though see its docstring for the upstream-parser
500 caveat observed in our captures.

Unsubscribe is exposed on this module as ``unsubscribe`` / ``unsubscribe_many``
(#31). They hit the misleadingly-named ``POST /api/1/webpage/bat/del`` —
per the 2026-05-31 finding, that single endpoint handles bulk-delete for
webpages, RSS subscriptions, and OPDS subscriptions uniformly. The
legacy flat-client aliases ``Boox.delete_webpages`` / ``Boox.unsubscribe``
remain in place for the original top-level scripts; the per-module
methods here are the Pattern A surface and call the same wire shape.
"""

from enum import IntEnum
from typing import Optional, Sequence

import requests

from boox.errors import from_response as _error_from_response


class FeedType(IntEnum):
    """Subscription source-type discriminator used by all subscribe endpoints.

    Boox's API encodes the feed flavor as an int on every request that
    touches a subscription. Callers pass ``FeedType.RSS`` / ``FeedType.OPDS``
    instead of raw ints — keeps call sites readable and stops typos like
    ``sourceType=1`` (which is the PushRead webpage type, *not* RSS).

    HAR-confirmed values:
    - ``RSS = 0`` (push-boox-har-2026-05-31.json, e.g.
      ``GET /api/1/subscribe/list?sourceType=0``).
    - ``OPDS = 2`` (same HAR, e.g.
      ``GET /api/1/subscribe/list?sourceType=2``).
    """

    RSS = 0
    OPDS = 2


class SubscriptionsClient:
    """Catalog-driven subscription surface attached to a ``BooxClient``."""

    def __init__(self, client):
        self._c = client

    # --------------------------- list_subscriptions ------------------------

    def list_subscriptions(
        self,
        source_type: int,
        limit: int = 100000,
        page: int = 1,
    ) -> dict:
        """Fetch the user's current subscriptions for one feed type.

        Issues ``GET /api/1/subscribe/list`` with
        ``sourceType=<int>&limit=<int>&page=<int>&sortBy=updatedAt``.
        Returns the parsed response envelope; the list of subscriptions
        lives under ``data.results`` (matches the HAR shape).

        ``source_type`` accepts ``FeedType`` members or their int values
        (``IntEnum`` cleanly serializes either way).

        The captured web UI requests use ``limit=100000`` — i.e. they pull
        everything in one page. We default to the same so the natural call
        ``list_subscriptions(FeedType.RSS)`` matches the captured behavior;
        callers wanting cheaper pagination override ``limit`` / ``page``.

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``GET https://push.boox.com/api/1/subscribe/list?limit=100000&page=1&sourceType=0&sortBy=updatedAt``.
        """
        return self._c.api_call(
            "subscribe/list",
            params={
                "sourceType": int(source_type),
                "limit": limit,
                "page": page,
                "sortBy": "updatedAt",
            },
        )

    # --------------------------- create_folder -----------------------------

    def create_folder(self, title: str, source_type: int) -> dict:
        """Create a subscription folder for the given feed type.

        Issues ``POST /api/1/subscribe/folder`` with
        ``{"title": <str>, "sourceType": <int>}``. Returns the parsed
        response envelope; the new folder doc — including its ``_id`` —
        lives under ``data``.

        ``source_type`` accepts ``FeedType`` members or their int values.

        HAR source: ``push-boox-har-2026-05-31.json`` entry
        ``POST https://push.boox.com/api/1/subscribe/folder`` with body
        ``{"title":"Test Group","sourceType":0}``.
        """
        return self._c.api_call(
            "subscribe/folder",
            data={"title": title, "sourceType": int(source_type)},
        )

    # --------------------------- subscribe ---------------------------------

    def subscribe(self, feed_id: str, parent_folder_id: str) -> dict:
        """Subscribe to a catalog feed under the given folder.

        Issues ``POST /api/1/subscribe/sub`` with
        ``{"parent": <folder_id>, "id": <feed_id>}``. Returns the parsed
        response envelope; the new user-sub record — including the
        ``_id`` consumed by ``unsubscribe`` (#31) — lives under ``data``.

        ``feed_id`` is the catalog-feed ``_id`` returned by
        ``search_catalog`` or ``recommended``. ``parent_folder_id`` is the
        folder ``_id`` returned by ``create_folder`` (or the ``_id`` of an
        existing folder fetched via ``list_subscriptions``).

        HAR source: ``push-boox-har-2026-05-31.json`` entry
        ``POST https://push.boox.com/api/1/subscribe/sub`` with body
        ``{"parent":"6a1c82baeef3164b0adb744e","id":"62ec90210f9f61452dcc6ddd"}``.
        """
        return self._c.api_call(
            "subscribe/sub",
            data={"parent": parent_folder_id, "id": feed_id},
        )

    # --------------------------- search_catalog ----------------------------

    def search_catalog(self, query: str, source_type: int) -> dict:
        """Search Boox's public feed catalog.

        Issues ``GET /api/1/rsses/public/search`` with
        ``text=<query>&sourceType=<int>``. Returns the parsed response
        envelope; matches live under ``data.results`` and the hit count
        under ``data.count``.

        Catalog-only: URLs not in Boox's public catalog return
        ``{"count": 0, "results": []}`` — not an error.

        HAR sources (both hit and miss):
        - hit: ``push-boox-har-2026-05-31.json`` entry
          ``GET /api/1/rsses/public/search?text=https:%2F%2Fspectrum.ieee.org%2Ffeeds%2Ffeed.rss&sourceType=0``
          returns ``count:1``.
        - miss: same HAR
          ``GET /api/1/rsses/public/search?text=https:%2F%2Fculpaeus.peacock-walleye.ts.net:8312%2Fopds&sourceType=2``
          returns ``count:0``.
        """
        return self._c.api_call(
            "rsses/public/search",
            params={"text": query, "sourceType": int(source_type)},
        )

    # --------------------------- recommended -------------------------------

    def recommended(self, source_type: int) -> dict:
        """Fetch Boox's curated catalog suggestions for one feed type.

        Issues ``GET /api/1/rsses/public/recommend`` with
        ``sourceType=<int>``. Returns the parsed response envelope; the
        suggestion list lives under ``data.results``.

        HAR source: ``push-boox-har-2026-05-31.json`` entries
        ``GET /api/1/rsses/public/recommend?sourceType=0`` and
        ``GET /api/1/rsses/public/recommend?sourceType=2``.
        """
        return self._c.api_call(
            "rsses/public/recommend",
            params={"sourceType": int(source_type)},
        )

    # --------------------------- feed_detail -------------------------------

    def feed_detail(self, feed_id: str) -> dict:
        """Fetch the catalog detail (and children, if a feed group) for a feed.

        Issues ``GET /api/1/rsses/one/detail`` with ``id=<feed_id>``. Returns
        the parsed response envelope; the feed (and its catalog-side
        ``children``) live under ``data``.

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``GET /api/1/rsses/one/detail?id=62eb87b40f9f61452dcc61b5``.
        """
        return self._c.api_call(
            "rsses/one/detail",
            params={"id": feed_id},
        )

    # --------------------------- preview_feed_url --------------------------

    def preview_feed_url(self, url: str, source_type: int) -> dict:
        """Ask Boox's server to fetch + parse a feed URL, returning a preview.

        Issues ``POST /api/1/rsses/url/content`` with the exact body shape the
        web UI sends:
        ``{"limit": 100000, "page": 1, "sourceType": <int>, "sortBy": "updatedAt", "url": <url>}``.
        Returns the parsed response envelope; the parsed-preview content
        (or an empty results list if the URL didn't parse) lives under
        ``data``.

        The ``limit`` / ``page`` / ``sortBy`` fields look like list-pagination
        params but the captured web flow always sends them on this endpoint
        regardless of how the call is used — preserved verbatim so the
        request shape matches the HAR. If a future HAR shows them dropped,
        narrow the body then.

        Useful for "validate this URL before saving" UX, but not required
        for the catalog-driven subscribe flow.

        HAR source: ``push-boox-har-2026-05-31.json`` entry
        ``POST /api/1/rsses/url/content`` with body
        ``{"limit":100000,"page":1,"sourceType":0,"sortBy":"updatedAt","url":"..."}``.
        """
        return self._c.api_call(
            "rsses/url/content",
            data={
                "limit": 100000,
                "page": 1,
                "sourceType": int(source_type),
                "sortBy": "updatedAt",
                "url": url,
            },
        )

    # --------------------------- unsubscribe -------------------------------

    def unsubscribe(self, sub_id: str) -> dict:
        """Unsubscribe from a single RSS or OPDS feed by user-sub record id.

        Thin wrapper around :meth:`unsubscribe_many` that forwards a
        one-element list, so single-item call sites read naturally.

        ``sub_id`` is the ``_id`` of a user-sub record (the document
        returned by :meth:`subscribe`, or any entry under
        ``list_subscriptions().data.results[*].children``). Not the
        catalog-feed ``_id`` and not a folder id.
        """
        return self.unsubscribe_many([sub_id])

    def unsubscribe_many(self, sub_ids: Sequence[str]) -> Optional[dict]:
        """Bulk-unsubscribe from RSS / OPDS feeds.

        Issues ``POST /api/1/webpage/bat/del`` with ``{"ids": [...]}``.
        Returns the parsed response envelope, e.g.
        ``{"result_code": 0, "data": "ok", "message": "SUCCESS"}``.

        Despite the ``webpage/bat/del`` name, this endpoint is the
        unified bulk-delete for webpages **and** RSS / OPDS user-sub
        records (and subscription folders) — per the 2026-05-31 finding,
        Boox routes all three categories through the same wire call.
        We expose it under two names for clarity at call sites:

        - :attr:`boox.Boox.delete_webpages` / :attr:`boox.Boox.unsubscribe`
          on the legacy flat client (kept verbatim).
        - :meth:`unsubscribe` / :meth:`unsubscribe_many` on this Pattern A
          module (preferred for new code).

        Empty-list behavior: returns ``None`` without hitting the server.
        The endpoint hasn't been HAR-captured with an empty ``ids``
        array, so rather than guess its handling we short-circuit
        client-side — saves a wasted round-trip and lets callers like
        ``unsubscribe_many([s for s in subs if cond])`` no-op cleanly
        when nothing matches their filter.

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``POST https://push.boox.com/api/1/webpage/bat/del`` with body
        ``{"ids":["6a1c9ce3961d4d4b17564650"]}``, response
        ``{"result_code":0,"data":"ok","message":"SUCCESS"}``.
        """
        ids = list(sub_ids)
        if not ids:
            return None
        return self._c.api_call("webpage/bat/del", data={"ids": ids})

    # --------------------------- export_opml -------------------------------

    def export_opml(self) -> bytes:
        """Export the user's RSS subscriptions as an OPML file, returning bytes.

        Two-step flow matching the captured web UI:

        1. ``POST /api/1/rsses/opml/export`` with an empty body. Response
           envelope has ``data`` set to a relative URL like
           ``/uploads/feed/export/<uuid>.opml`` pointing at the freshly-
           generated OPML.
        2. ``GET https://<cloud>/<relative_url>`` — fetch the OPML file
           itself. The captured browser GET sends no Authorization header
           (the UUID-in-path acts as a capability token); we mirror that.

        Returns the raw file bytes. Deliberately no client-side XML /
        OPML validation: Boox sometimes returns non-OPML content when the
        last import was bad (the 2026-05-31 captures show HTML coming
        back after an accidental HTML "OPML" upload). Callers that need
        to verify validity should parse the returned bytes themselves.

        HAR sources:
        - ``push-boox-har-2026-05-31.json`` entries 241 / 242
          (``POST .../rsses/opml/export`` followed by
          ``GET .../uploads/feed/export/6ee90060-...opml``).
        - ``rss-subscribe-har-2026-05-31.json`` entries 73 / 74
          (same flow, different UUID).
        """
        cloud = self._c.cloud
        token = self._c.token

        # Step 1: POST /api/1/rsses/opml/export — empty body, Bearer auth.
        # ``BooxClient.api_call`` always serializes its ``data`` arg to
        # JSON (even ``{}`` becomes the literal string ``"{}"``); the
        # captured request has ``Content-Length: 0``. To match the HAR
        # shape exactly we issue the POST directly here.
        export_url = f"https://{cloud}/api/1/rsses/opml/export"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.post(export_url, headers=headers)
        exc = _error_from_response(r)
        if exc is not None:
            raise exc
        relative_url = r.json()["data"]

        # Step 2: GET the generated OPML file. Captured browser flow does
        # not send Authorization on this hop — the per-export UUID in the
        # path is the only access control. We mirror that and rely on the
        # transport-layer ``raise_for_status`` for non-2xx; the file is
        # static content, not a result-code-envelope JSON response.
        file_url = f"https://{cloud}{relative_url}"
        f = requests.get(file_url)
        f.raise_for_status()
        return f.content

    # --------------------------- import_opml -------------------------------

    def import_opml(
        self,
        opml_bytes: bytes,
        *,
        filename: str = "subscriptions.opml",
    ) -> dict:
        """Import OPML feeds via the bulk-subscribe workaround for custom URLs.

        Issues ``POST /api/1/rsses/opml/import`` as
        ``multipart/form-data`` with a single ``file`` field carrying
        ``opml_bytes``. Returns the parsed response envelope on success.

        The captured browser request uses ``Content-Type:
        text/x-opml+xml`` for the file part; we mirror it. ``filename``
        is included in the multipart header to match the browser shape
        but isn't load-bearing — Boox parses by content, not name.

        **Fragility warning.** The upstream OPML parser is brittle:
        both attempts in our 2026-05-31 capture
        (``rss-subscribe-har-2026-05-31.json`` entries 87 / 88) returned
        HTTP 500 with ``{"result_code": 1, "message": "Attribute without
        value\\nLine: 6\\nColumn: 19\\nChar: s"}`` — the server was
        trying to parse an HTML page that had been uploaded by accident
        as if it were OPML. Real OPML may parse cleanly, but the
        endpoint clearly has rough edges. ``api_call``'s shared
        ``from_response`` mapping handles the 500: it raises
        :class:`boox.errors.APIError` with the upstream parser message
        preserved verbatim on the exception's ``response_body`` so
        callers can surface it without re-running the request.

        No ``source_type`` parameter: the captured wire shape carries
        only the ``file`` field, with no ``sourceType`` discriminator.
        We don't speculate about how a future HAR might encode one — if
        Boox grows multi-type OPML support, we add the parameter then.

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``POST https://push.boox.com/api/1/rsses/opml/import`` with
        ``multipart/form-data`` body ``name="file"; filename="...opml";
        Content-Type: text/x-opml+xml``.
        """
        cloud = self._c.cloud
        token = self._c.token

        # Like ``export_opml``, bypass ``api_call``: that helper always
        # JSON-serializes its body, but this endpoint needs a multipart
        # upload with a typed file part. We still route the response
        # through ``from_response`` so the 500-with-parse-error path
        # raises the same typed ``APIError`` as the rest of the client.
        url = f"https://{cloud}/api/1/rsses/opml/import"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        files = {"file": (filename, opml_bytes, "text/x-opml+xml")}
        r = requests.post(url, headers=headers, files=files)
        exc = _error_from_response(r)
        if exc is not None:
            raise exc
        return r.json()


__all__ = ["FeedType", "SubscriptionsClient"]
