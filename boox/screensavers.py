"""Screensavers module — push, list, delete device-sleep images.

Pattern A subobject (project decision #6, locked 2026-05-31): wired as
``self.screensavers = ScreensaversClient(self)`` on ``BooxClient``.

Surface overview
----------------

Screensavers are images shown on the Boox device when it sleeps. The
2026-05-31 web-UI HAR settled the probe: pushing a new screensaver uses
the dedicated ``POST /api/1/screenSavers/push`` endpoint with a
``cbMsg`` callback ID linking to a prior ``/neocloud/_bulk_docs``
``contentType: "push_screensaver"`` registration. Listing and deletion
fall back to the shared BooxDrop ``push/message`` surface filtered to
``sourceType=100`` — the same listing endpoint :mod:`boox.files` uses,
just with a category filter. The wrapper here keeps that boundary clean
so callers don't need to remember which surface to hit.

Endpoint coverage
-----------------

- ``GET  /api/1/config/stss``                — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entries 13/16).
- ``POST <oss-endpoint>/<key>`` (multipart)  — HAR-confirmed (same capture, OSS multipart shared with files surface).
- ``POST /neocloud/_bulk_docs``              — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entry 101, ``contentType: "push_screensaver"``).
- ``POST /api/1/screenSavers/push``          — HAR-confirmed (booxdrop-upload-har-2026-05-31.json entry 104).
- ``GET  /api/1/push/message``               — **HAR-inferred, live-confirmed** for screensavers. Entry 104 response carries ``sourceType: 100``; live smoke (``tests/test_live_screensavers.py``, 2026-06-01) confirms the same ``push/message`` listing surface :mod:`boox.files` uses returns screensaver entries with that filter. No standalone screensaver-listing HAR exists; if a future capture reveals a dedicated ``screenSavers/list`` path, narrow this wrapper then.
- ``POST /api/1/push/message/batchDelete``   — **HAR-inferred, live-confirmed** for the screensaver category. Per #33 body "likely … same as regular files"; live smoke confirms the BooxDrop delete endpoint accepts screensaver ids and removes them from the listing. If a future capture reveals a dedicated path, narrow this wrapper then.

Legacy push path
----------------

The earlier ``push/saveAndPush`` + ``sourceType:100`` flow that older
community RE notes describe is **not** what the web UI uses post-2026.
We don't ship a wrapper for it: the dedicated ``screenSavers/push``
endpoint is the confirmed-current surface (see ``docs/endpoint-inventory.md``).
"""

import json
import logging
import os
import uuid
from typing import Optional, Sequence

import oss2
import requests

from boox.errors import OSSError


class ScreensaversClient:
    """Screensavers push/list/delete attached to a ``BooxClient``."""

    def __init__(self, client):
        self._c = client

    # --------------------------- push_screensaver -------------------------

    def push_screensaver(
        self,
        image_path: str,
        title: Optional[str] = None,
    ) -> dict:
        """Upload a local image as a Boox device screensaver, end-to-end.

        Orchestrates the four-step web-UI push flow:

        1. ``GET /api/1/config/stss`` — fetch OSS STS credentials.
        2. OSS multipart upload — ``oss2.resumable_upload`` against the
           per-user push key ``<user_uid>/push/<uuid><.ext>`` (same key
           layout as BooxDrop files; the OSS namespace doesn't separate
           screensavers from regular files). ``oss2.exceptions.OssError``
           wraps in :class:`boox.errors.OSSError`.
        3. :meth:`_register_screensaver_doc` — POST to
           ``/neocloud/_bulk_docs`` with ``contentType: "push_screensaver"``
           against the ``<user_uid>-MESSAGE`` Sync Gateway channel.
           **Auth is the ``SyncGatewaySession`` cookie, not Bearer JWT** —
           ``/neocloud/*`` rejects Bearer. Without this step the
           subsequent ``screenSavers/push`` rejects the ``cbMsg``.
        4. ``POST /api/1/screenSavers/push`` — Bearer-authed; carries
           the ``data`` envelope (name, resourceKey, bucket, resourceType,
           title, parent) plus a ``cbMsg`` with the bulk_docs ``id`` /
           ``rev`` pair so Boox can join the two records server-side.

        ``title`` overrides the on-device display title (defaults to
        the file's basename). Returns the parsed ``screenSavers/push``
        response envelope — its ``data._id`` is the screensaver record
        id (distinct from the bulk_docs ``_id``), suitable for later
        passing to :meth:`delete_screensavers`.

        Behavior when the SyncGatewaySession cookie isn't available:
        skips step 3 with a logged warning. The ``screenSavers/push``
        call still fires but the device may filter the screensaver
        out as NaN-timestamped (same pattern as :meth:`FilesClient.push_file`).
        Provision via ``BOOX_SYNC_TOKEN`` or rely on the Phase 1 #27
        runtime mint.

        HAR sources:

        - ``booxdrop-upload-har-2026-05-31.json`` entries 101 (bulk_docs
          ``push_screensaver`` doc) and 104 (``screenSavers/push``).
        - ``booxdrop-repush-har-2026-05-31.json`` confirms the per-user
          OSS key convention for a second user.
        """
        client = self._c

        # Step 1: STS credentials.
        stss_data = client.api_call("config/stss")["data"]

        # Step 2: OSS multipart upload.
        auth = oss2.Auth(
            stss_data["AccessKeyId"], stss_data["AccessKeySecret"]
        )
        bucket = oss2.Bucket(auth, client.endpoint, client.bucket_name)

        _, ext = os.path.splitext(image_path)
        resource_type = ext.lstrip(".").lower() if ext else "bin"
        file_uuid = uuid.uuid4()
        remote_key = f"{client.userid}/push/{file_uuid}{ext}"
        sts_headers = {"x-oss-security-token": stss_data["SecurityToken"]}
        try:
            oss2.resumable_upload(
                bucket, remote_key, image_path, headers=sts_headers
            )
        except oss2.exceptions.OssError as oss_exc:
            raise OSSError(
                f"OSS upload failed for {remote_key}: {oss_exc}"
            ) from oss_exc

        # Step 3: bulk_docs MESSAGE-channel registration.
        file_size = os.path.getsize(image_path)
        file_mtime_ms = int(os.path.getmtime(image_path) * 1000)
        basename = os.path.basename(image_path)
        display_title = title if title is not None else basename
        doc_id = str(file_uuid).replace("-", "")
        doc_rev = f"1-{uuid.uuid4().hex}"

        if client.sync_token:
            self._register_screensaver_doc(
                doc_id=doc_id,
                doc_rev=doc_rev,
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
                "push_screensaver: sync_token unset — skipping bulk_docs "
                "registration; screensaver may appear with NaN timestamps. "
                "Provision BOOX_SYNC_TOKEN to enable.",
            )

        # Step 4: screenSavers/push. Bearer-authed via ``api_call``.
        return client.api_call(
            "screenSavers/push",
            headers={
                "Content-Type": "application/json;charset=utf-8",
            },
            data={
                "data": {
                    "bucket": client.bucket_name,
                    "name": basename,
                    "parent": None,
                    "resourceDisplayName": basename,
                    "resourceKey": remote_key,
                    "resourceType": resource_type,
                    "title": display_title,
                },
                "cbMsg": {
                    "id": doc_id,
                    "rev": doc_rev,
                },
            },
        )

    def _register_screensaver_doc(
        self,
        doc_id: str,
        doc_rev: str,
        filename: str,
        filesize: int,
        remote_key: str,
        resource_type: str,
        created_at: int,
        updated_at: int,
        title: str,
    ) -> dict:
        """POST a ``push_screensaver`` doc to ``<user_uid>-MESSAGE`` via Sync Gateway.

        Mirrors :meth:`boox.files.FilesClient._register_in_message_channel`
        with two deltas: ``contentType`` is ``"push_screensaver"`` (not
        ``"digital_content"``) and the inner content carries ``title`` +
        ``updatedAt`` as HAR entry 101 shows. ``doc_rev`` is taken as a
        parameter (rather than generated here) so the caller can hand
        the same ``id`` / ``rev`` pair to the ``screenSavers/push``
        ``cbMsg`` envelope.

        Auth: the ``SyncGatewaySession`` cookie. ``/neocloud/*`` rejects
        Bearer JWT, hence the explicit ``requests.post`` rather than
        going through ``api_call``.

        HAR source: ``booxdrop-upload-har-2026-05-31.json`` entry 101.
        """
        client = self._c
        bulkdata = {
            "docs": [{
                "contentType": "push_screensaver",
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
                "_rev": doc_rev,
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

    # --------------------------- list_screensavers ------------------------

    def list_screensavers(self, limit: int = 30, offset: int = 0) -> list:
        """List screensaver entries on the device.

        Issues ``GET /api/1/push/message`` with the JSON-encoded
        ``where`` filter pinned to ``sourceType=100`` (screensavers).
        Returns the parsed listing under the envelope's ``list`` key;
        entries share the shape :meth:`FilesClient.list_files` returns
        (``{"data": {"args": {"_id": ..., "name": ..., "formats": [...],
        "storage": {<fmt>: {"oss": {...}}}, ...}}}``).

        **HAR-inferred surface.** No standalone screensaver-listing HAR
        was captured. The choice of endpoint + filter is grounded in
        the ``screenSavers/push`` response (HAR entry 104) which marks
        the freshly-pushed screensaver as ``sourceType: 100`` — i.e.
        the same listing surface BooxDrop uses with that category
        filter returns screensavers. If a dedicated ``screenSavers/list``
        endpoint surfaces in a future capture, narrow this wrapper then.
        """
        client = self._c
        where = {
            "limit": limit,
            "offset": offset,
            "sourceType": 100,
        }
        body = client.api_call(
            "push/message",
            params={"where": json.dumps(where, separators=(",", ":"))},
        )
        return body["list"]

    # --------------------------- delete_screensavers ----------------------

    def delete_screensavers(self, ids: Sequence[str]) -> Optional[dict]:
        """Bulk-delete screensavers by ``_id``.

        Issues ``POST /api/1/push/message/batchDelete`` with
        ``{"ids": [...]}`` (Bearer auth). Returns the parsed response
        envelope; on success Boox returns ``{"result_code": 0,
        "data": "ok"}``. Empty list short-circuits to ``None`` without
        hitting the server (matches
        :meth:`FilesClient.delete_files` /
        :meth:`SubscriptionsClient.unsubscribe_many` conventions).

        ``ids`` is a list of screensaver ``_id`` strings (the
        ``data.args._id`` field on :meth:`list_screensavers` entries).

        **HAR-inferred surface for the screensaver category.** Per the
        #33 issue body, delete "likely … same as regular files" because
        the screensaver entries live on the same ``push/message`` store
        the BooxDrop files do. If a future capture reveals a dedicated
        ``screenSavers/delete`` path, narrow this wrapper then.
        """
        id_list = list(ids)
        if not id_list:
            return None
        return self._c.api_call(
            "push/message/batchDelete",
            data={"ids": id_list},
        )


__all__ = ["ScreensaversClient"]
