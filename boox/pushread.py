"""PushRead webpage CRUD — list, delete (and push_url, pending HAR capture).

Pattern A subobject (project decision #6, locked 2026-05-31): wired as
``self.pushread = PushRead(self)`` on ``BooxClient``. Methods compose against
``self._c.api_call(...)``.

Endpoint coverage at first cut (2026-05-31):

- ``GET  /api/1/webpage/list``    — HAR-confirmed in two captures.
- ``POST /api/1/webpage/bat/del`` — HAR-confirmed in rss-subscribe-har.
- ``POST /api/1/webpage/url``     — **HAR-missing.** Captured a webpage-add
  flow has not been recorded yet; the endpoint name is only referenced as a
  string inside the push.boox.com JS bundle. ``push_url`` is intentionally
  not implemented until a HAR is captured so the request body shape can be
  copied verbatim rather than guessed. See #29 review thread for status.

The legacy ``Boox.delete_webpages`` / ``Boox.unsubscribe`` methods on the
flat client class are kept untouched: they share the same HTTP shape and
remain the canonical entry points for hrw-style scripts until the layout
migration in #45 splits the client.
"""

from typing import Sequence


class PushRead:
    """PushRead webpage CRUD attached to a ``BooxClient`` instance."""

    def __init__(self, client):
        self._c = client

    def list_webpages(self, limit: int = 30, page: int = 1) -> dict:
        """Fetch a page of saved PushRead webpages.

        Issues ``GET /api/1/webpage/list`` with the documented query string
        (``limit``, ``page``, ``orderBy=-1``, ``sortBy=updatedAt``) — newest
        first. Returns the parsed response envelope; the list of entries
        lives under the ``list`` key (matches the HAR shape).

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``GET https://push.boox.com/api/1/webpage/list?limit=30&page=1&orderBy=-1&sortBy=updatedAt``.
        """
        return self._c.api_call(
            "webpage/list",
            params={
                "limit": limit,
                "page": page,
                "orderBy": -1,
                "sortBy": "updatedAt",
            },
        )

    def delete_webpages(self, ids: Sequence[str]) -> dict:
        """Bulk-delete PushRead webpages by id.

        Issues ``POST /api/1/webpage/bat/del`` with ``{"ids": [...]}``.
        Returns the parsed response envelope.

        The endpoint is misleadingly named: per the 2026-05-31 finding it
        handles webpages, RSS subscriptions, and OPDS subscriptions
        uniformly. The legacy ``Boox.delete_webpages`` and
        ``Boox.unsubscribe`` methods on the flat client both route here too;
        the intent-specific naming on those wrappers makes call-site reads
        clearer when used for non-webpage deletes.

        HAR source: ``rss-subscribe-har-2026-05-31.json`` entry
        ``POST https://push.boox.com/api/1/webpage/bat/del``.
        """
        return self._c.api_call("webpage/bat/del", data={"ids": list(ids)})


__all__ = ["PushRead"]
