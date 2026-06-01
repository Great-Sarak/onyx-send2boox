"""Files module — BooxDrop CRUD (push, list, delete) and cloud-files download.

Pattern A subobject (project decision #6, locked 2026-05-31): wired as
``self.files = FilesClient(self)`` on ``BooxClient``. Methods compose
against ``self._c.api_call(...)`` for Bearer-authed REST calls and reach
into ``self._c`` directly for OSS uploads / Sync Gateway cookie posts.

Two surfaces kept deliberately distinct in this module
------------------------------------------------------

Boox exposes two file stores; they share neither IDs nor endpoints, and
this module's docstrings call out which surface each method touches:

- **BooxDrop** — the device's "inbox" for arbitrary documents. Pushed
  via ``push/saveAndPush`` (after an OSS upload + ``/neocloud/_bulk_docs``
  MESSAGE-channel registration), listed via ``push/message``, deleted via
  ``push/message/batchDelete``. ``push_file`` / ``list_files`` /
  ``delete_files`` operate on this surface.

- **cloud-files** — a separate cloud-stored object set (listed via
  ``GET /api/1/cloudFiles``; per-item download via
  ``GET /api/1/cloudFiles/download/one``). We don't yet have a captured
  flow showing what populates it, but the endpoint exists in the JS
  bundle and the settings HAR confirms listing. ``download_file``
  operates on this surface.

Do **not** mix them: a ``_id`` returned by ``list_files`` is not a valid
``file_id`` for ``download_file``, and vice versa. If you need to
re-fetch a just-pushed BooxDrop file's bytes, use the signed OSS URL
stored in the listing entry's ``data.args.storage.<fmt>.oss.url`` — see
the live smoke fixture in ``tests/test_live_smoke.py`` for an example.

Endpoint coverage
-----------------

- ``GET  /api/1/config/stss``              — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entries 13/16).
- ``POST <oss-endpoint>/<key>`` (multipart) — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entries 35/37/39).
- ``POST /neocloud/_bulk_docs``            — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entry 45).
- ``POST /api/1/push/saveAndPush``         — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entry 48).
- ``GET  /api/1/push/message``             — HAR-confirmed (booxdrop-repush-har-2026-05-31.json).
- ``POST /api/1/push/message/batchDelete`` — HAR-confirmed (push-boox-har-2026-05-31.json).
- ``GET  /api/1/cloudFiles/download/one``  — **bundle-referenced, not HAR-confirmed**.
  The endpoint is defined in ``static/js/index-S4JVDm45.js`` as
  ``a.get("/api/1/cloudFiles/download/one", {params: e})`` (entry 120 of
  push-boox-har-2026-05-31.json), but no actual request appears in any
  capture. Param keys and response shape inferred from sibling endpoints
  (Boox's "one" endpoints uniformly take ``id=<file_id>`` and return
  ``data`` as a signed OSS URL string or a ``{url, ...}`` object). See
  the ``download_file`` docstring for the limitation and the follow-up
  capture issue.

`move_file` — deliberately NOT exposed
--------------------------------------

The issue (#32) called for exploring a ``move_file`` method. No move /
changeParent / moveTo endpoint appears in any captured HAR or in the
push.boox.com JS bundle (only "moveTo" UI translation strings — not
endpoint paths). Per the HAR-first standing rule (and the Phase 0 retro
flagging community/bundle inference as the primary failure mode), we
do not ship a speculative wrapper. If a future capture reveals a real
``push/message/move`` (or equivalent) endpoint, add the method then.
"""

import json
import logging
import os
import uuid
from typing import Any, Optional, Sequence

import oss2
import requests

from boox.errors import OSSError


class FilesClient:
    """BooxDrop CRUD + cloud-files download surface attached to a ``BooxClient``."""

    def __init__(self, client):
        self._c = client

    # --------------------------- push_file ---------------------------------

    def push_file(
        self,
        path: str,
        title: Optional[str] = None,
        parent: Optional[str] = None,
    ) -> dict:
        """Upload a local file to **BooxDrop** end-to-end.

        Orchestrates the three-step web-UI push flow as one coherent call:

        1. ``GET /api/1/config/stss`` — fetch OSS STS credentials
           (Bearer JWT auth). Yields ``AccessKeyId`` / ``AccessKeySecret``
           / ``SecurityToken`` for the upload.
        2. OSS multipart upload — ``oss2.resumable_upload`` against the
           per-user push key ``<user_uid>/push/<uuid><.ext>``. Wraps
           ``oss2.exceptions.OssError`` in :class:`boox.errors.OSSError`
           so callers don't need to import oss2 to catch upload failures
           (#28).
        3. :meth:`_register_in_message_channel` — POST to
           ``/neocloud/_bulk_docs`` against the ``<user_uid>-MESSAGE``
           Sync Gateway channel, carrying ``createdAt`` + ``updatedAt`` as
           epoch-ms ints from the source file's mtime. **Auth here is the
           ``SyncGatewaySession`` cookie, not Bearer JWT** — ``/neocloud/*``
           rejects Bearer. Without this step the reader filters the file
           out as NaN-timestamped (Phase 0 #5 bug fix).
        4. ``POST /api/1/push/saveAndPush`` — Bearer-authed, registers
           the file with Boox cloud. ``resourceType`` is derived from the
           file extension (Phase 0 #5 fix to hrw's hardcoded ``"txt"``).

        ``title`` overrides the on-device display title (defaults to the
        file's basename). ``parent`` is the BooxDrop folder ``_id`` to
        place the file under (None = top-level inbox); folder support
        isn't load-bearing for any captured flow, so it's passed through
        without client-side validation.

        Returns the parsed ``push/saveAndPush`` response envelope.

        Behavior when the SyncGatewaySession cookie isn't available:
        skips step 3 with a logged warning. The file uploads + registers
        with ``saveAndPush`` but may appear with NaN timestamps in the
        reader. Provision via ``BOOX_SYNC_TOKEN`` in config (or rely on
        the Phase 1 #27 runtime mint).

        HAR sources:

        - ``booxdrop-upload-har-2026-05-31.json`` — full reference flow
          for a single .pdf push (entries 13/16 STS → 35/37/39 OSS
          multipart → 45 bulk_docs → 48 saveAndPush).
        - ``booxdrop-repush-har-2026-05-31.json`` — second push for the
          same user, confirms the per-user OSS-key convention.
        - ``push-boox-har-2026-05-31.json`` — saveAndPush appears in
          context with other ops.
        """
        client = self._c

        # Step 1: STS credentials. Bearer-authed via ``api_call``.
        stss_data = client.api_call("config/stss")["data"]

        # Step 2: OSS multipart upload to the per-user push key.
        auth = oss2.Auth(
            stss_data["AccessKeyId"], stss_data["AccessKeySecret"]
        )
        bucket = oss2.Bucket(auth, client.endpoint, client.bucket_name)

        _, ext = os.path.splitext(path)
        # Phase 0 #5: derive resourceType from extension (was hardcoded
        # "txt"). Fall back to "bin" for dotless files.
        resource_type = ext.lstrip(".").lower() if ext else "bin"
        file_uuid = uuid.uuid4()
        # Phase 0 #7: ``ext`` already carries the leading dot, so don't
        # add another separator dot ("<uuid>..pdf" was the pre-fix bug).
        remote_key = f"{client.userid}/push/{file_uuid}{ext}"
        sts_headers = {"x-oss-security-token": stss_data["SecurityToken"]}
        try:
            oss2.resumable_upload(bucket, remote_key, path, headers=sts_headers)
        except oss2.exceptions.OssError as oss_exc:
            raise OSSError(
                f"OSS upload failed for {remote_key}: {oss_exc}"
            ) from oss_exc

        # Step 3: bulk_docs MESSAGE-channel registration (Phase 0 #5).
        # Stat after upload so a slow filesystem isn't an issue.
        file_size = os.path.getsize(path)
        file_mtime_ms = int(os.path.getmtime(path) * 1000)
        basename = os.path.basename(path)
        display_title = title if title is not None else basename

        if client.sync_token:
            self._register_in_message_channel(
                doc_id=str(file_uuid).replace("-", ""),
                filename=basename,
                filesize=file_size,
                remote_key=remote_key,
                resource_type=resource_type,
                created_at=file_mtime_ms,
                updated_at=file_mtime_ms,
                title=display_title,
            )
        else:
            logging.warning(
                "push_file: sync_token unset — skipping bulk_docs "
                "registration; file may appear with NaN timestamps in "
                "the reader. Provision BOOX_SYNC_TOKEN to enable.",
            )

        # Step 4: saveAndPush. Bearer-authed via ``api_call``.
        return client.api_call(
            "push/saveAndPush",
            headers={
                "Content-Type": "application/json;charset=utf-8",
            },
            data={
                "data": {
                    "bucket": client.bucket_name,
                    "name": basename,
                    "parent": parent,
                    "resourceDisplayName": basename,
                    "resourceKey": remote_key,
                    "resourceType": resource_type,
                    "title": display_title,
                }
            },
        )

    def _register_in_message_channel(
        self,
        doc_id: str,
        filename: str,
        filesize: int,
        remote_key: str,
        resource_type: str,
        created_at: int,
        updated_at: int,
        title: str,
    ) -> dict:
        """POST a ``digital_content`` doc to ``<user_uid>-MESSAGE`` via Sync Gateway.

        Phase 0 #5 landed this inline in ``BooxClient._push_message_doc``
        as a fix to the NaN-timestamp bug; #32 formalizes it as the
        module-internal step 3 of :meth:`push_file`. Behavior is
        identical — kept here so the three-step push reads as one
        coherent function.

        Auth: the ``SyncGatewaySession`` cookie minted at startup by
        the Phase 1 #27 ``auth.mint_sync_session`` (or cached via
        ``BOOX_SYNC_TOKEN``). ``/neocloud/*`` rejects Bearer JWT, hence
        the explicit ``requests.post`` here rather than going through
        ``api_call``.

        HAR source: ``booxdrop-upload-har-2026-05-31.json`` entry 45.
        """
        client = self._c
        bulkdata = {
            "docs": [{
                "contentType": "digital_content",
                "content": json.dumps({
                    "_id": doc_id,
                    "createdAt": created_at,
                    "distributeChannel": "onyx",
                    "formats": [resource_type],
                    "guid": doc_id,
                    "name": filename,
                    "ownerId": client.userid,
                    "size": filesize,
                    "md5": "",
                    "storage": {
                        resource_type: {
                            "oss": {
                                "displayName": filename,
                                "expires": 0,
                                "key": remote_key,
                                "provider": "oss",
                                "size": filesize,
                            },
                        },
                    },
                    "title": title,
                    "updatedAt": updated_at,
                }),
                "msgType": 2,
                "dbId": f"{client.userid}-MESSAGE",
                "user": client.userid,
                "name": filename,
                "size": filesize,
                "uniqueId": doc_id,
                "createdAt": created_at,
                "updatedAt": updated_at,
                "_id": doc_id,
                "_rev": f"1-{uuid.uuid4().hex}",
            }],
            "new_edits": False,
        }
        url = f"https://{client.cloud}/neocloud/_bulk_docs"
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"SyncGatewaySession={client.sync_token}",
        }
        r = requests.post(url, headers=headers, json=bulkdata)
        r.raise_for_status()
        return r.json()

    # --------------------------- list_files --------------------------------

    def list_files(
        self,
        limit: int = 30,
        offset: int = 0,
        parent: Any = 0,
        source_type: Optional[int] = None,
    ) -> list:
        """List BooxDrop files (the ``push/message`` surface).

        Issues ``GET /api/1/push/message`` with a JSON-encoded ``where``
        query param carrying ``limit`` / ``offset`` / ``parent`` (and
        optionally ``sourceType``). Returns the parsed list under the
        response envelope's ``list`` key — entries have the shape
        ``{"data": {"args": {"_id": ..., "name": ..., "formats": [...],
        "storage": {<fmt>: {"oss": {...}}}, "createdAt": ...,
        "updatedAt": ...}}}``.

        Unlike the legacy :meth:`boox.BooxClient.list_files`, this method
        does **not** print a table to stdout; it returns the parsed list
        directly. Callers who want the table can format it themselves
        (or stick with the legacy method on the flat client).

        ``source_type`` filters the listing to one document category:

        - omitted (default) — all BooxDrop documents.
        - ``100`` — screensaver images (separate surface; left for #33).
        - other ints — reserved by Boox for future categories.

        ``parent`` filters to a folder (folder ``_id`` string) or to the
        top-level inbox (``0``, the default). Phase 0 #6's listing fix
        is preserved: JSON-encoded ``where`` filter rather than manual
        string concat.

        HAR sources:
        - ``booxdrop-repush-har-2026-05-31.json`` — listing of the
          user's BooxDrop files after a fresh push.
        - ``push-boox-har-2026-05-31.json`` — listing in context with
          other operations.
        """
        client = self._c
        where: dict = {"limit": limit, "offset": offset, "parent": parent}
        if source_type is not None:
            where["sourceType"] = source_type
        body = client.api_call(
            "push/message",
            params={"where": json.dumps(where, separators=(",", ":"))},
        )
        return body["list"]

    # --------------------------- download_file -----------------------------

    def download_file(
        self,
        file_id: str,
        out_path: Optional[str] = None,
    ) -> bytes:
        """Download a **cloud-files** object by id, returning its bytes.

        .. warning::

           This method hits the ``cloudFiles`` surface, **not** BooxDrop's
           ``push/message`` surface. ``file_id`` here is a cloudFiles
           ``_id``; it is **not** the ``_id`` returned by
           :meth:`list_files`. The two stores share neither IDs nor
           wire endpoints.

           To re-fetch a just-pushed **BooxDrop** file's bytes, use the
           signed OSS URL stored in its listing entry at
           ``entry["data"]["args"]["storage"][<fmt>]["oss"]["url"]`` —
           that URL is the same one the device reader uses. See the
           live smoke fixture in ``tests/test_live_smoke.py`` for the
           pattern.

        .. note::

           **Endpoint shape is bundle-referenced, not HAR-confirmed.**

           The push.boox.com JS bundle (entry 120 of
           ``push-boox-har-2026-05-31.json``) defines
           ``Ce = e => a.get("/api/1/cloudFiles/download/one", {params: e})``,
           so the path + method are known. No captured HAR exercises
           the endpoint — every other ``cloudFiles`` call we have is the
           plain listing. Param key + response shape here are inferred
           from sibling "one"-style Boox endpoints (e.g.
           ``rsses/one/detail`` uses ``id=<feed_id>`` and returns
           ``data`` as the resource), and the bundle name
           ``getExtensionUrl`` suggests the response is a signed OSS URL.

           If the live smoke test reveals a different shape, narrow
           this wrapper then — don't speculate further now. The
           follow-up capture issue tracks the gap.

        Two-step flow:

        1. ``GET /api/1/cloudFiles/download/one?id=<file_id>`` (Bearer
           auth) — Boox responds with a parsed envelope whose ``data``
           field carries the signed OSS URL. We accept either form:
           a bare string, or a ``{"url": ...}`` object (we read ``url``
           if it's an object, else use the string verbatim — covers
           both common Boox-cloud conventions).
        2. ``GET <signed_oss_url>`` — fetch the file bytes from OSS.
           The OSS URL has a short expiration (~3 hours per the
           BooxDrop HAR captures) so we don't cache it.

        Returns the raw bytes. If ``out_path`` is provided, also writes
        the bytes to that path.

        HAR source: **none — endpoint is bundle-referenced only**, see
        the note above.
        """
        client = self._c
        envelope = client.api_call(
            "cloudFiles/download/one",
            params={"id": file_id},
        )
        data = envelope.get("data")
        if isinstance(data, dict):
            signed_url = data.get("url")
        else:
            signed_url = data
        if not isinstance(signed_url, str) or not signed_url:
            raise ValueError(
                f"cloudFiles/download/one returned no usable URL: "
                f"data={data!r}"
            )

        # OSS fetch — no Bearer header (the signed URL carries its own
        # access credentials in query params). raise_for_status surfaces
        # non-2xx as plain transport errors, not boox APIError envelopes.
        r = requests.get(signed_url)
        r.raise_for_status()
        content = r.content

        if out_path is not None:
            with open(out_path, "wb") as fh:
                fh.write(content)

        return content

    # --------------------------- delete_files ------------------------------

    def delete_files(self, ids: Sequence[str]) -> dict:
        """Bulk-delete BooxDrop files by ``_id``.

        Issues ``POST /api/1/push/message/batchDelete`` with
        ``{"ids": [...]}`` (Bearer auth). Returns the parsed response
        envelope; on success Boox returns ``{"result_code": 0,
        "data": "ok"}``.

        ``ids`` is a list of BooxDrop file ``_id`` strings (e.g. from
        ``list_files()`` entries' ``data.args._id``). Not cloudFiles
        ids — see :meth:`download_file`'s docstring for the surface
        distinction. Empty list short-circuits client-side to ``None``
        without hitting the server (preserves the
        :class:`boox.subscriptions.SubscriptionsClient.unsubscribe_many`
        convention; saves a round-trip when the caller's filter
        returns nothing).

        The legacy :attr:`boox.BooxClient.delete_files` method on the
        flat client remains in place for hrw-style scripts; it hits the
        same wire shape.

        HAR source: ``push-boox-har-2026-05-31.json`` — referenced in
        endpoint inventory, exercised by Phase 0 #8 tests.
        """
        id_list = list(ids)
        if not id_list:
            return None
        return self._c.api_call(
            "push/message/batchDelete",
            data={"ids": id_list},
        )


__all__ = ["FilesClient"]
