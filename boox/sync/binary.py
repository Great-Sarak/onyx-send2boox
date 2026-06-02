"""Per-user binary fetch — note templates, point groups, book thumbnails, files.

HAR audit (``tools/boox/captures/`` 2026-05-31) established the auth surface
for these binaries against ground truth:

- ``notes-har-2026-05-31.har``: 388 OSS hits on ``aliyuncs.com/<uid>/note/...``;
  **zero** Bearer-JWT hits against ``*.boox.com/<uid>/note/...``.
- ``library-base-har-2026-05-31.json``: 1309 OSS hits on
  ``aliyuncs.com/<uid>/reader/...``; **zero** Bearer-JWT hits.

So per-user binaries (page templates, ink point groups, book thumbnails,
sideloaded book files) live on direct Alibaba OSS, authed with the same
STS credentials :meth:`BooxClient.send_file` uses for uploads. This module
fetches them via :mod:`oss2`.

The ``library-base`` HAR shows 0 GETs / 718 HEADs on the
thumbnail/book paths — the web client only existence-checks these in that
capture. The GET path is shape-inferred (OSS GET shares the URL shape with
HEAD; auth is identical). A capture with actual binary GETs would tighten
confidence; tracked as a follow-up but not blocking.

Cross-surface — do not conflate
-------------------------------

A separate Bearer-authenticated endpoint, ``push.boox.com/api/1/cloudFiles/download/one``,
serves BooxDrop binary downloads. That lives in :mod:`boox.files` (Phase 2
#32, PR #57; wire-shape confirmation tracked in #58). This module only
handles per-user OSS keys for notes and reader content.

URL encoding for point-group keys
---------------------------------

Point-group OSS keys embed two literal ``#`` characters::

    <user_uid>/note/<note_id>/point/<page_uuid>#<point_group_uuid>#points

The HAR shows these as ``%23`` on the wire (URL fragment markers aren't
sent to servers, so unencoded ``#`` would silently truncate the request
path). :func:`oss2.Bucket.get_object` doesn't percent-encode keys for us,
so the fragment-bearing segment is quoted explicitly with
``urllib.parse.quote(..., safe='')``.

404 policy
----------

Thumbnail + book file return ``None`` on 404 — sideloaded books often
have no cloud thumbnail or no cloud-stored binary, and surfacing this as
``None`` keeps call sites tidy. This is documented behavior, not
undefined.

Note template + point group raise :class:`boox.errors.NotFoundError` on
404 — those are device-authored cloud artifacts; their absence is a real
signal worth bubbling.

API placement
-------------

Standalone functions, not a Pattern A subobject on ``BooxClient.sync``.
These are four utility fetches over distinct OSS keys that share only an
auth pattern; callers invoke rarely with ``user_uid`` + ``doc_id`` already
in hand. A ``client.sync.binary`` namespace doesn't pay for itself for
four functions. PR body calls out the choice.

STS refresh
-----------

Each call fetches fresh STS creds via ``config/stss``. The tokens are
short-lived; caching across calls would buy little and add invalidation
complexity that Phase 5 #41 is already on the hook for. If
``client.token`` (the Bearer JWT) is unset, we raise :class:`AuthError`
before hitting OSS — fetching STS itself requires Bearer auth.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import oss2
from oss2.exceptions import NoSuchKey, NotFound

from boox.errors import AuthError


__all__ = [
    "fetch_note_template",
    "fetch_note_point_group",
    "fetch_book_thumbnail",
    "fetch_book_file",
]


def _oss_bucket(client) -> oss2.Bucket:
    """Build an :class:`oss2.Bucket` authed with fresh STS credentials.

    Mirrors the auth path in :meth:`BooxClient.send_file` but uses
    :class:`oss2.StsAuth` (which carries the security token in the
    signature) rather than ``oss2.Auth`` + an ``x-oss-security-token``
    header. Either works on the wire; ``StsAuth`` is the cleaner choice
    for GETs where we aren't passing custom headers anyway.
    """
    if not client.token:
        raise AuthError(
            "client.token is unset; load a Bearer JWT via boox.read_config "
            "+ BooxClient(config) before fetching binaries (STS credentials "
            "are minted from the JWT via config/stss)"
        )
    stss = client.api_call("config/stss")["data"]
    auth = oss2.StsAuth(
        stss["AccessKeyId"],
        stss["AccessKeySecret"],
        stss["SecurityToken"],
    )
    return oss2.Bucket(auth, client.endpoint, client.bucket_name)


def _get_bytes(client, key: str) -> bytes:
    bucket = _oss_bucket(client)
    return bucket.get_object(key).read()


def _get_bytes_or_none(client, key: str) -> Optional[bytes]:
    bucket = _oss_bucket(client)
    try:
        result = bucket.get_object(key)
    except (NoSuchKey, NotFound):
        return None
    return result.read()


def fetch_note_template(
    client, user_uid: str, note_id: str, page_uuid: str
) -> bytes:
    """Fetch a page-template JSON blob for a note page.

    OSS key::

        <user_uid>/note/<note_id>/template/json/<page_uuid>.template_json

    Returns raw bytes — the HAR shows ``Content-Type: application/json``
    on the response but we don't auto-decode; the caller can ``json.loads``
    if they want the structure or store as-is if they want a faithful
    archive.
    """
    key = f"{user_uid}/note/{note_id}/template/json/{page_uuid}.template_json"
    return _get_bytes(client, key)


def fetch_note_point_group(
    client,
    user_uid: str,
    note_id: str,
    page_uuid: str,
    point_group_uuid: str,
) -> bytes:
    """Fetch a point-group ink binary for a note page.

    OSS key has the literal form::

        <user_uid>/note/<note_id>/point/<page_uuid>#<point_group_uuid>#points

    The two ``#`` characters are part of the key — they must be
    percent-encoded as ``%23`` on the request URL or the path will be
    silently truncated at the first ``#`` (URL fragment semantics).
    :mod:`oss2` does not encode keys for us, so we quote the
    fragment-bearing segment explicitly.
    """
    segment = quote(f"{page_uuid}#{point_group_uuid}#points", safe="")
    key = f"{user_uid}/note/{note_id}/point/{segment}"
    return _get_bytes(client, key)


def fetch_book_thumbnail(
    client, user_uid: str, book_uuid: str, ext: str = "png"
) -> Optional[bytes]:
    """Fetch a book thumbnail, or ``None`` if absent.

    OSS key::

        <user_uid>/reader/<book_uuid>/thumbnail/<book_uuid>.<ext>

    Sideloaded books frequently lack a cloud thumbnail (the device never
    uploaded one); returning ``None`` on 404 lets callers branch without
    a try/except. Documented behavior, not undefined.
    """
    key = f"{user_uid}/reader/{book_uuid}/thumbnail/{book_uuid}.{ext}"
    return _get_bytes_or_none(client, key)


def fetch_book_file(
    client, user_uid: str, book_uuid: str, ext: str
) -> Optional[bytes]:
    """Fetch a book binary file, or ``None`` if absent.

    OSS key::

        <user_uid>/reader/<book_uuid>/book/<book_uuid>.<ext>

    Same ``None``-on-404 semantics as :func:`fetch_book_thumbnail` —
    sideloaded books whose binary never reached the cloud return 404 here,
    which is normal not exceptional.
    """
    key = f"{user_uid}/reader/{book_uuid}/book/{book_uuid}.{ext}"
    return _get_bytes_or_none(client, key)
