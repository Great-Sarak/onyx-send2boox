"""PushRead webpage CRUD — list, push_url, delete.

Pattern A subobject (project decision #6, locked 2026-05-31): wired as
``self.pushread = PushRead(self)`` on ``BooxClient``. Methods compose against
``self._c.api_call(...)``.

Endpoint coverage:

- ``GET  /api/1/webpage/list``    — HAR-confirmed (rss-subscribe-har-2026-05-31.json).
- ``POST /api/1/webpage/url``     — HAR-confirmed (pushread-add-har-2026-05-31.json).
- ``POST /api/1/webpage/bat/del`` — HAR-confirmed (rss-subscribe-har-2026-05-31.json).

The legacy ``Boox.delete_webpages`` / ``Boox.unsubscribe`` methods on the
flat client class are kept untouched: they share the same HTTP shape and
remain the canonical entry points for hrw-style scripts until the layout
migration in #45 splits the client.
"""

from typing import Optional, Sequence


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

    def push_url(self, url: str, parent_folder: Optional[str] = None) -> dict:
        """Push a URL to PushRead for Boox-side fetch + parse.

        Issues ``POST /api/1/webpage/url`` with
        ``{"url": <url>, "parentFolder": <parent_folder>}``. The captured
        flow uses ``"parentFolder": null`` for top-level adds; non-null
        folder ids haven't been captured yet, so ``parent_folder`` is passed
        through verbatim without client-side validation.

        Returns the parsed response envelope. On success the new webpage's
        ``_id`` (usable in ``delete_webpages``) lives at
        ``result["data"]["_id"]``; the full Boox-fetched-and-parsed entry
        (title, description, cover, etc.) is alongside it under ``data``.

        HAR source: ``pushread-add-har-2026-05-31.json`` entry 0
        ``POST https://push.boox.com/api/1/webpage/url``.
        """
        return self._c.api_call(
            "webpage/url",
            data={"url": url, "parentFolder": parent_folder},
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
