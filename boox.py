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

        # Sync Gateway session cookie — used for /neocloud/* calls, set
        # alongside the Bearer JWT from the same browser harvest. Optional
        # so existing code paths that don't touch Sync Gateway keep working
        # without it. Both ConfigParser SectionProxy and plain dict expose
        # .get(); .get('sync_token') returns None when the key is absent.
        self.sync_token = config['default'].get('sync_token') or None

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

        r = requests.request(method,
                             f'https://{self.cloud}/{api}/{api_url}',
                             headers=headers,
                             params=params,
                             data=json.dumps(data))

        logging.info(json.dumps(r.json(), indent=4))
        logging.info('')

        return r.json()

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
        remotename = f'{self.userid}/push/{file_uuid}.{extension_with_dot}'

        token_headers = {'x-oss-security-token': self.security_token}

        oss2.resumable_upload(bucket, remotename, filepath,
                              headers=token_headers)

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
        self.api_call('push/message/batchDelete', data={"ids": ids})
