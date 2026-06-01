# Boox cloud API — Endpoint Inventory

Tri-state coverage map of every Boox cloud endpoint we know about. Refresh whenever a new HAR is captured or new JS bundles are observed.

## States

| State | Meaning |
| --- | --- |
| **har-confirmed** | Observed in one of our captures under `tools/boox/captures/`. Request / response shape known with high confidence. |
| **bundle-referenced** | Present as a literal in the push.boox.com JS bundles (extracted 2026-05-31 from `static/js/index-S4JVDm45.js` + the login chunk). Endpoint exists; the web UI can call it; shape unknown until we capture a HAR. |
| **community-documented** | Present in [hrw/onyx-send2boox#API.md](https://github.com/hrw/onyx-send2boox/blob/main/API.md) or [gian-didom/onyx-send2boox](https://github.com/gian-didom/onyx-send2boox), but not in our HARs and not in our bundle grep. Treat as "exists but may have drifted." |

When implementing a module:

- **har-confirmed:** shape is known, write code directly.
- **bundle-referenced:** capture a HAR of the flow first; don't depend on shape from the bundle alone.
- **community-documented:** capture or probe first; the docs may be stale.

## Endpoint inventory

### `/api/1/*` — REST surface (Bearer auth except where noted)

| Method | Path | State | Notes |
| --- | --- | --- | --- |
| GET | `/api/1/appVersions/android/one` | bundle-referenced | Android app version probe |
| GET | `/api/1/auth/qrcode/check` | bundle-referenced | QR-code login state |
| POST | `/api/1/auth/qrcode/create` | bundle-referenced | QR-code login init |
| POST | `/api/1/cb/setFeedbackRead` | har-confirmed | Mark feedback dialog read (settings HAR 2026-05-31) |
| GET | `/api/1/cloudFiles` | har-confirmed | List cloud-stored files (not BooxDrop) (settings HAR 2026-05-31) |
| GET | `/api/1/cloudFiles/download/one` | bundle-referenced | Download one cloud file |
| GET | `/api/1/config/buckets` | har-confirmed | OSS bucket / endpoint info (init chain) |
| GET | `/api/1/config/stss` | har-confirmed | STS credentials for OSS upload |
| GET | `/api/1/configUsers/one` | har-confirmed | User config snapshot |
| POST | `/api/1/devices/disable/token` | bundle-referenced | Disable a device token |
| POST | `/api/1/devices/lock` | har-confirmed | Body `{"id": <device_id>, "type": "lock"\|"unlock"}` (settings HAR 2026-05-31) |
| POST | `/api/1/devices/lock/description` | bundle-referenced | Set lock-message text |
| GET | `/api/1/faqs` | har-confirmed | FAQ list (settings HAR 2026-05-31) |
| GET | `/api/1/faqs/category/index` | har-confirmed | FAQ categories (settings HAR 2026-05-31) |
| GET | `/api/1/favs` | har-confirmed | Favorites listing |
| POST | `/api/1/giftCards/active/gift` | bundle-referenced | Activate gift card |
| POST | `/api/1/giftCards/check/code` | bundle-referenced | Validate gift code |
| POST | `/api/1/giftCards/copy/code` | bundle-referenced | |
| GET | `/api/1/giftCards/my/gift` | bundle-referenced | List my gift cards |
| GET | `/api/1/im/getFeedbackDialog` | har-confirmed | (settings HAR 2026-05-31) |
| GET | `/api/1/im/getSig` | community-documented | hrw's init chain — IM signature (purpose unknown) |
| GET | `/api/1/loginAds/show/one` | bundle-referenced | Login-page ad |
| GET | `/api/1/manualGroups` | har-confirmed | Device-manual listing (settings HAR 2026-05-31) |
| POST | `/api/1/pcDownloads` | bundle-referenced | Desktop app download tracking |
| POST | `/api/1/push/message/batchDelete` | har-confirmed | Delete BooxDrop files (NB: same endpoint, "files" semantic) |
| POST | `/api/1/push/saveAndPush` | har-confirmed | Notify Boox cloud of a BooxDrop upload |
| POST | `/api/1/rsses/opml/export` | har-confirmed | Export user's RSS/OPDS as OPML; returns relative file URL |
| POST | `/api/1/rsses/opml/import` | har-confirmed | Bulk subscribe via OPML (multipart/form-data) |
| GET | `/api/1/rsses/one/detail` | har-confirmed | Feed detail by `_id` |
| GET | `/api/1/rsses/public/recommend` | har-confirmed | Boox-curated feed recommendations |
| GET | `/api/1/rsses/public/search` | har-confirmed | Catalog search by URL/text |
| POST | `/api/1/rsses/url/content` | har-confirmed | Boox-side fetch + parse of a feed URL (validate/preview) |
| POST | `/api/1/screenSavers/push` | har-confirmed | Push screensaver image (newer than the push/saveAndPush+sourceType=100 path) |
| GET | `/api/1/serverInfos` | har-confirmed | Server build info |
| POST | `/api/1/statistics/del/userdata` | har-confirmed | **Broken server-side** — returns 500 `{result_code: 1, message: "No Server!"}` (settings HAR 2026-05-31) |
| GET | `/api/1/statistics/personalUsageAmount` | bundle-referenced | Account-level usage stats |
| POST | `/api/1/statistics/readInfoList` | bundle-referenced | Reading statistics POST |
| GET | `/api/1/statistics/v2/user/storage` | har-confirmed | Storage usage (settings HAR 2026-05-31) |
| POST | `/api/1/subscribe/folder` | har-confirmed | Create a subscription folder |
| GET | `/api/1/subscribe/list` | har-confirmed | List subscriptions (filter by sourceType) |
| POST | `/api/1/subscribe/sub` | har-confirmed | Subscribe a catalog feed under a folder |
| GET | `/api/1/sysNotices` | har-confirmed | System notices |
| GET | `/api/1/updateLogs` | har-confirmed | Changelog notifications |
| POST | `/api/1/users/avatar` | bundle-referenced | Upload avatar |
| GET | `/api/1/users/checkPhoneOrEmail` | bundle-referenced | Existence check pre-signup |
| POST | `/api/1/users/destroy/account` | bundle-referenced | Account deletion |
| GET | `/api/1/users/generateAccessToken` | bundle-referenced | Mint an access token (purpose: device-pair?) |
| GET | `/api/1/users/getDevice` | har-confirmed | Device list (init chain) |
| GET | `/api/1/users/me` | har-confirmed | Authenticated account info (init chain) |
| POST | `/api/1/users/me/resetPwd` | bundle-referenced | Password reset |
| POST | `/api/1/users/removeDevice` | bundle-referenced | Unlink a device |
| POST | `/api/1/users/removePhoneOrEmail` | bundle-referenced | Unlink contact |
| POST | `/api/1/users/resetInfoConfirm` | bundle-referenced | |
| POST | `/api/1/users/sendVerifyCode` | bundle-referenced | **Captcha-gated** SMS / email code sender (deferred to Phase 5 — see `flora/BOOX.md` §"Refresh procedure"). |
| POST | `/api/1/users/signupByPhoneOrEmail` | bundle-referenced | Exchange verification code for JWT |
| GET | `/api/1/users/syncToken` | har-confirmed | **Mints the SyncGatewaySession cookie from the Bearer JWT.** Returns `{session_id, expires (~3 weeks), cookie_name: "SyncGatewaySession", channels: []}` (settings HAR 2026-05-31). **Big Phase 5 simplification:** we never need to harvest the cookie separately — Bearer JWT is the sole manually-refreshed credential; the cookie is derived at runtime. Phase 1 auth module (#27) will call this on startup. |
| POST | `/api/1/users/unbindAssociatedAccount` | bundle-referenced | |
| PUT | `/api/1/users/updateInfo` | har-confirmed | Edit profile fields (settings HAR 2026-05-31) |
| POST | `/api/1/users/updatePhoneOrEmail` | bundle-referenced | Change contact |
| GET | `/api/1/users/v2/me/calibration/size` | bundle-referenced | Device display calibration (probe in Phase 5) |
| POST | `/api/1/webpage/bat/del` | har-confirmed | **Unified bulk-delete** — handles webpages, RSS subs, OPDS subs (misleadingly-named) |
| GET | `/api/1/webpage/list` | har-confirmed | List PushRead webpages |
| POST | `/api/1/webpage/url` | har-confirmed | Push a URL to PushRead — `{"url": "...", "parentFolder": null}`; response carries the full Boox-parsed entry under `data` (incl. `_id` usable in `webpage/bat/del`). HAR: `pushread-add-har-2026-05-31.json`. |

### `/api/v2/*`

| Method | Path | State | Notes |
| --- | --- | --- | --- |
| POST | `/api/v2/users/login` | bundle-referenced | Newer login endpoint (alongside the legacy `users/signupByPhoneOrEmail`) — also captcha-gated. |

### `/neocloud/*` — Couchbase Sync Gateway (SyncGatewaySession cookie auth — NOT Bearer)

| Method | Path | State | Notes |
| --- | --- | --- | --- |
| GET | `/neocloud/` | har-confirmed | Sync Gateway db info (db_name, server_uuid, state) |
| GET | `/neocloud/<doc_id>` | har-confirmed | Get a single doc — used as fallback when `_bulk_get` returns 406 |
| GET | `/neocloud/_changes` | har-confirmed | Replication change feed (longpoll + bychannel filter) |
| POST | `/neocloud/_bulk_docs` | har-confirmed | Push a batch of docs; used for register-file (MESSAGE channel) and edits to NOTE_TREE/READER_LIBRARY |
| POST | `/neocloud/_bulk_get` | har-confirmed | Batch fetch docs by revs |
| POST | `/neocloud/_revs_diff` | har-confirmed | Replication: ask server what revs it doesn't have |
| GET, PUT | `/neocloud/_local/<checkpoint>` | har-confirmed | Replication checkpoint persistence |

### `/uploads/feed/export/<uuid>.opml`

| Method | Path | State | Notes |
| --- | --- | --- | --- |
| GET | `/uploads/feed/export/<uuid>.opml` | har-confirmed | OPML export download URL (returned from `rsses/opml/export`) |

### OSS / Aliyun (separate auth: STS via `config/stss`)

| Method | Path | State | Notes |
| --- | --- | --- | --- |
| POST | `<oss>/<user_uid>/push/<uuid>.<ext>?uploads=` | har-confirmed | Init multipart upload |
| PUT | `<oss>/<user_uid>/push/<uuid>.<ext>?partNumber=N&uploadId=ID` | har-confirmed | Upload multipart part |
| POST | `<oss>/<user_uid>/push/<uuid>.<ext>?uploadId=ID` | har-confirmed | Complete multipart upload |
| GET | `<oss>/?list-type=2&prefix=<user_uid>/<area>/<doc>` | har-confirmed | List OSS objects for a given doc (used in perma-delete) |

## Channels seen on `/neocloud/`

`channels=<user_uid>-<CHANNEL_NAME>` query param of `_changes`:

| Channel | Purpose | First HAR |
| --- | --- | --- |
| `NOTE_TREE` | Note metadata (incl. folders + operation records) | notes-har |
| `READER_LIBRARY` | Book metadata, reader-notes (annotations), book ops | library-base-har |
| `MESSAGE` | File-push registration (with timestamps) — written before saveAndPush | booxdrop-upload-har |

## Update protocol

When adding to this file:

1. **Source-attribute** every change (e.g., "From booxdrop-upload-har-2026-05-31") so we can audit drift later.
2. **HAR > bundle > community** — promote the state aggressively when a new capture confirms shape.
3. **Don't delete entries that drop out of the bundle**; mark as `community-documented` and note the date.
