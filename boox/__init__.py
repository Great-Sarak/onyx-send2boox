#  SPDX-License-Identifier: MIT

import configparser
import json
import locale
import logging
import os
import time
import oss2
import requests
import uuid

from boox.auth import Auth
from boox.errors import (
    APIError,
    AuthError,
    BooxError,
    NotFoundError,
    OSSError,
    RateLimitError,
    from_response as _error_from_response,
)
from boox.pushread import PushRead


def read_config(filename="config.ini"):
    config = configparser.ConfigParser()
    config.read(filename)

    return config


class Boox:

    def __init__(self, config, code=None, skip_init=False,
                 show_log=False):

        if show_log:
            logging.basicConfig(level=logging.NOTSET)

        if config['default']['cloud']:
            self.cloud = config['default']['cloud']
        else:
            self.cloud = 'eur.boox.com'

        # Pattern A wiring (project decision #6, locked 2026-05-31): every
        # functional module surfaces as a subobject on the client. ``auth``
        # is the first; ``pushread`` follows (#29); ``files`` / etc. land in
        # later phases.
        self.auth = Auth(self)
        self.pushread = PushRead(self)

        # Cached SyncGatewaySession (fallback only — Phase 1 #27 derives
        # this at runtime from the Bearer JWT). Read here so it's available
        # if the runtime mint fails. Both ConfigParser SectionProxy and
        # plain dict expose ``.get()``.
        cached_sync_token = config['default'].get('sync_token') or None
        self.sync_token = cached_sync_token

        if skip_init:
            self.token = False
        else:
            if config['default']['token']:
                self.token = config['default']['token']
            elif config['default']['email'] and code:
                self.token = False
                self.login_with_email(config['default']['email'], code)

            self.userid = self.api_call('users/me')['data']['uid']

            self.api_call('users/getDevice')
            self.api_call('im/getSig', params={"user": self.userid})

            onyx_cloud = self.api_call('config/buckets')['data']['onyx-cloud']

            self.bucket_name = onyx_cloud['bucket']
            self.endpoint = onyx_cloud['aliEndpoint']

            # Runtime-mint the SyncGatewaySession cookie from the Bearer
            # JWT (#27). If the mint call fails, fall back to the cached
            # value if any — preserves the Phase 0 behavior where the
            # cookie was loaded from config — and emit a warning so the
            # divergence isn't silent. If neither path produces a token,
            # /neocloud/* calls will fail with a clearer message at the
            # call site (send_file warns explicitly).
            try:
                self.auth.mint_sync_session()
            except (AuthError, requests.RequestException) as exc:
                if cached_sync_token:
                    logging.warning(
                        "syncToken mint failed (%s); falling back to "
                        "cached BOOX_SYNC_TOKEN from config",
                        exc,
                    )
                    self.sync_token = cached_sync_token
                else:
                    logging.warning(
                        "syncToken mint failed (%s) and no cached "
                        "BOOX_SYNC_TOKEN; /neocloud/* calls will be "
                        "unauthenticated",
                        exc,
                    )

    def login_with_email(self, email, code):

        self.token = self.api_call('users/signupByPhoneOrEmail',
                                   data={'mobi': email,
                                         'code': code})['data']['token']

    def api_call(self, api_url, method='GET', headers={}, data={}, params={},
                 api='api/1'):

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        if data:
            headers['Content-Type'] = 'application/json;charset=utf-8'
            method = 'POST'

        # ``requests.RequestException`` (ConnectionError, Timeout, etc.)
        # deliberately propagates unchanged — see boox/errors.py docstring
        # for the rationale. Callers that need to recover from transport
        # failures catch ``requests.RequestException``; callers that need
        # to recover from API failures catch ``BooxError``.
        r = requests.request(method,
                             f'https://{self.cloud}/{api}/{api_url}',
                             headers=headers,
                             params=params,
                             data=json.dumps(data))

        # Map HTTP status + result_code to a typed exception (#28).
        exc = _error_from_response(r)
        if exc is not None:
            raise exc

        body = r.json()
        logging.info(json.dumps(body, indent=4))
        logging.info('')

        return body

    def list_files(self, limit=24, offset=0, source_type=None, parent=0):
        """Fetch a BooxDrop listing; print human-readable + return parsed list.

        Phase 0 #6 fixes:
        - Drops the ``locale.setlocale(locale.LC_ALL, locale.getlocale()[0])``
          call that crashed in any env where ``locale.getlocale()`` returned
          ``(None, None)`` (minimal containers, distros without LC_ALL set).
        - Replaces the ``:>10n`` locale-dependent thousands separator with
          ``:>10,`` (Python builtin — works in any locale).
        - Switches the ``where`` filter from manual string concat to
          ``json.dumps`` (encodes correctly and pulls in ``source_type``
          / ``parent`` cleanly).
        - Accepts ``source_type`` so callers can filter to screensavers
          (``source_type=100``) or future categories.
        - Returns the parsed list so tests / callers can inspect results.
        """
        where = {"limit": limit, "offset": offset, "parent": parent}
        if source_type is not None:
            where["sourceType"] = source_type

        files = self.api_call(
            'push/message',
            params={"where": json.dumps(where, separators=(',', ':'))},
        )['list']

        print("        ID               |    Size    | Name")
        print("-------------------------|------------|"
              "-------------------------------------------------------")

        for entry in files:
            data = entry['data']['args']
            fmt = data['formats'][0]
            size = int(data['storage'][fmt]['oss']['size'])
            print(f"{data['_id']} | {size:>10,} | {data['name']}")

        return files

    def send_file(self, filename):
        stss_data = self.api_call('config/stss')['data']

        self.access_key_id = stss_data['AccessKeyId']
        self.access_key_secret = stss_data['AccessKeySecret']
        self.security_token = stss_data['SecurityToken']

        auth = oss2.Auth(self.access_key_id, self.access_key_secret)

        bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)

        filepath = filename  # preserve original path for stat() below
        _tmp, extension_with_dot = os.path.splitext(filename)
        # Phase 0 #5: derive resourceType from extension (was hardcoded "txt"
        # which caused all uploads to be classified as text in the reader).
        # Fall back to "bin" for dotless files. The OSS-key double-dot fix
        # is targeted separately in #7.
        resource_type = (
            extension_with_dot.lstrip('.').lower() if extension_with_dot else 'bin'
        )
        file_uuid = uuid.uuid4()
        # Phase 0 #7: OSS key format — extension_with_dot already includes
        # the leading dot, so concatenate without a separator dot. The old
        # f'.../{uuid}.{extension_with_dot}' produced `<uuid>..pdf` (two
        # dots) which doesn't match what the web UI generates.
        remotename = f'{self.userid}/push/{file_uuid}{extension_with_dot}'

        token_headers = {'x-oss-security-token': self.security_token}

        # Wrap oss2 failures (AccessDenied / network / etc.) in OSSError so
        # callers don't need to import oss2 just to catch upload errors
        # (#28). The original exception is preserved as ``__cause__``.
        try:
            oss2.resumable_upload(bucket, remotename, filepath,
                                  headers=token_headers)
        except oss2.exceptions.OssError as oss_exc:
            raise OSSError(
                f"OSS upload failed for {remotename}: {oss_exc}"
            ) from oss_exc

        # File metadata for the bulk_docs registration below.
        file_size = os.path.getsize(filepath)
        # File mtime as both createdAt and updatedAt — better than the web UI's
        # upload-time-only convention. The reader displays updatedAt as
        # "modified"; falling back to current time only when mtime isn't
        # available (e.g., streamed input — not our case here).
        file_mtime_ms = int(os.path.getmtime(filepath) * 1000)
        filename = os.path.basename(filepath)

        # Phase 0 #5: register the file in the Sync Gateway MESSAGE channel
        # so it carries valid timestamps. Without this, push/saveAndPush
        # records the file but the reader filters it out as NaN-timestamped.
        # Bearer JWT is *not* valid here — /neocloud/* uses the
        # SyncGatewaySession cookie.
        if self.sync_token:
            self._push_message_doc(
                doc_id=str(file_uuid).replace('-', ''),
                filename=filename,
                filesize=file_size,
                remotename=remotename,
                resource_type=resource_type,
                created_at=file_mtime_ms,
                updated_at=file_mtime_ms,
            )
        else:
            logging.warning(
                "send_file: sync_token unset — skipping bulk_docs registration; "
                "file may appear with NaN timestamps in the reader. Add "
                "BOOX_SYNC_TOKEN to your config to enable.",
            )

        self.api_call('push/saveAndPush',
                      headers={
                          'Content-Type': 'application/json;charset=utf-8',
                      },
                      data={
                          "data": {
                              "bucket": self.bucket_name,
                              'name': filename,
                              'parent': None,
                              'resourceDisplayName': filename,
                              "resourceKey": remotename,
                              "resourceType": resource_type,
                              "title": filename}
                      })

    def _push_message_doc(self, doc_id, filename, filesize, remotename,
                          resource_type, created_at, updated_at):
        """POST a digital_content doc to <user_uid>-MESSAGE via Sync Gateway.

        Phase 0 #5: the web-UI upload flow does this *before* push/saveAndPush
        to set valid createdAt/updatedAt timestamps. The reader filters out
        files lacking these. Auth is via the SyncGatewaySession cookie, not
        the Bearer JWT.
        """
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
                    "ownerId": self.userid,
                    "size": filesize,
                    "md5": "",
                    "storage": {
                        resource_type: {
                            "oss": {
                                "displayName": filename,
                                "expires": 0,
                                "key": remotename,
                                "provider": "oss",
                                "size": filesize,
                            },
                        },
                    },
                }),
                "msgType": 2,
                "dbId": f"{self.userid}-MESSAGE",
                "user": self.userid,
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
        url = f'https://{self.cloud}/neocloud/_bulk_docs'
        headers = {
            'Content-Type': 'application/json',
            'Cookie': f'SyncGatewaySession={self.sync_token}',
        }
        r = requests.post(url, headers=headers, json=bulkdata)
        r.raise_for_status()
        return r.json()

    def request_verification_code(self, email):
        self.api_call('users/sendMobileCode', data={"mobi": email})

    def delete_files(self, ids):
        """Bulk-delete BooxDrop files by id.

        ``ids`` is a list of file ``_id`` strings (e.g. from ``list_files()``
        entries' ``data.args._id``). Returns the parsed ``api_call`` response.
        """
        return self.api_call('push/message/batchDelete', data={"ids": ids})

    def delete_webpages(self, ids):
        """Bulk-delete PushRead webpages, RSS subscriptions, or OPDS subs.

        Endpoint is misleadingly named ``webpage/bat/del`` internally; per the
        2026-05-31 finding it handles all three types uniformly (the
        Sorotassu/Rukha capture saw RSS unsubscribe go through this endpoint
        with no separate "unsubscribe" path).
        """
        return self.api_call('webpage/bat/del', data={"ids": ids})

    def unsubscribe(self, sub_ids):
        """Unsubscribe from RSS/OPDS feeds by user-sub record id.

        Alias for ``delete_webpages`` — kept as a separate name so callers'
        intent stays clear at call sites.
        """
        return self.delete_webpages(sub_ids)


# Canonical client name from Phase 1 onward. ``Boox`` is kept as a legacy
# alias so hrw's top-level scripts (``send_file.py``, ``delete_files.py``,
# etc.) keep working until they're migrated. #45 splits this module body
# into ``boox/client.py`` + ``boox/_http.py``; for now both names point at
# the same wholesale class moved from the flat ``boox.py``.
BooxClient = Boox

from boox._version import __version__  # noqa: E402

__all__ = [
    "APIError",
    "AuthError",
    "Boox",
    "BooxClient",
    "BooxError",
    "NotFoundError",
    "OSSError",
    "RateLimitError",
    "read_config",
    "__version__",
]
