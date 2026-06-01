"""Subscriptions module — catalog ops, folders, list, recommend, detail, preview.

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

Catalog-only limitation: the web UI subscribe flow only succeeds for feeds
already in Boox's curated public catalog. ``search_catalog`` returns
``count: 0`` for URLs not in the catalog (HAR-confirmed against
``https://www.atlassian.com/blog/rss``, ``https://culpaeus.peacock-walleye.ts.net:8312/opds``,
and several others). The OPML-import workaround for custom URLs lives in
``#31`` (next issue). This affects the user's priority #2 (custom feed
ingestion) — callers wanting custom URLs go through OPML import, not this
module.

Unsubscribe lives on the legacy flat-client surface (``Boox.unsubscribe`` /
``Boox.delete_webpages``) until ``#31`` migrates it to a per-module method.
"""

from enum import IntEnum
from typing import Optional, Sequence


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


__all__ = ["FeedType", "SubscriptionsClient"]
