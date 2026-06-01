"""Live smoke for the screensavers module — push → list → delete round-trip.

Drives :class:`boox.screensavers.ScreensaversClient` against the live
Boox cloud using a tiny synthetic PNG (generated in-process via
``struct.pack`` so we don't ship a binary asset). Probes the inferred
``push/message`` listing + ``push/message/batchDelete`` surfaces from
the issue body so we discover any wire-shape divergence before locking
the wrapper.

Requires both:
- BOOX_TOKEN (Bearer JWT for /api/1/*)
- BOOX_SYNC_TOKEN (SyncGatewaySession cookie for /neocloud/*)

Skipped by default; run with:

    BOOX_RUN_LIVE_TESTS=1 \\
    BOOX_SECRETS_FILE=/path/to/secrets/boox.env \\
    pytest -m live tests/test_live_screensavers.py -v

Added by #33.
"""

import struct
import time
import uuid
import zlib

import pytest

import boox


@pytest.fixture
def live_client(live_token, live_sync_token):
    """Boox client wired to the live cloud with full init chain executed."""
    config = {
        "default": {
            "cloud": "push.boox.com",
            "token": live_token,
            "sync_token": live_sync_token,
        }
    }
    return boox.Boox(config)


def _make_solid_png(width: int, height: int, rgb: tuple) -> bytes:
    """Build a minimal solid-color PNG without depending on PIL.

    Manual PNG assembly: IHDR + IDAT (raw filter byte 0 per row, zlib
    compressed) + IEND. The synthesizer keeps us off the binary-asset
    rail per the issue body's "tiny synthetic PNG" guidance.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Each scanline = filter byte (0 = none) + width * 3 bytes RGB.
    row = bytes([0]) + bytes(rgb) * width
    raw = row * height
    idat = zlib.compress(raw, 9)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def tracked_smoke_png(tmp_path):
    """Write a tiny solid-magenta PNG with a distinctive name."""
    name = f"pytest-screensaver-{uuid.uuid4().hex[:12]}.png"
    path = tmp_path / name
    path.write_bytes(_make_solid_png(100, 100, (255, 0, 255)))
    return path


def _find_by_name(entries, name):
    """Return the listing entry matching ``name`` or None."""
    for entry in entries:
        args = entry.get("data", {}).get("args", {})
        if args.get("name") == name:
            return entry
    return None


def _poll_for_listing(client, filename, *, present, timeout_s=20, interval_s=2):
    """Re-list every ``interval_s`` until ``filename`` matches ``present``."""
    deadline = time.monotonic() + timeout_s
    last_entries = []
    while time.monotonic() < deadline:
        last_entries = client.screensavers.list_screensavers(limit=50)
        entry = _find_by_name(last_entries, filename)
        if (entry is not None) == present:
            return entry, last_entries
        time.sleep(interval_s)
    return _find_by_name(last_entries, filename), last_entries


@pytest.mark.live
def test_live_screensavers_push_list_delete_roundtrip(
    live_client, tracked_smoke_png
):
    """End-to-end: push a screensaver, confirm it lists under
    ``sourceType=100``, delete it, confirm it's gone. Cleans up on any
    failure path.

    This is the de-facto validation for the HAR-inferred listing +
    delete surfaces (see :mod:`boox.screensavers` module docstring).
    """
    filename = tracked_smoke_png.name
    pushed_id = None

    try:
        push_resp = live_client.screensavers.push_screensaver(
            str(tracked_smoke_png)
        )
        assert push_resp.get("result_code") == 0, (
            f"screenSavers/push returned non-zero: {push_resp}"
        )

        # Poll until the screensaver appears in the list_screensavers
        # surface (inferred to be push/message with sourceType=100).
        entry, entries = _poll_for_listing(
            live_client, filename, present=True, timeout_s=20
        )
        assert entry is not None, (
            f"Just-pushed screensaver {filename!r} not in list of "
            f"{len(entries)} entries after 20s — listing surface may "
            f"not be push/message with sourceType=100. Check whether "
            f"the response shape matches BooxDrop's push/message "
            f"listing (data.args.name)."
        )

        args = entry["data"]["args"]
        pushed_id = args["_id"]
        # sourceType=100 should be on the entry (it's how we filtered).
        assert args.get("sourceType") == 100, (
            f"sourceType regression: expected 100, got "
            f"{args.get('sourceType')!r} on entry {args!r}"
        )
        assert "png" in args.get("formats", []), (
            f"resourceType regression: formats={args.get('formats')!r}"
        )

        del_resp = live_client.screensavers.delete_screensavers([pushed_id])
        assert del_resp.get("result_code") == 0, (
            f"delete_screensavers returned non-zero: {del_resp} — "
            f"delete surface may not be push/message/batchDelete for "
            f"screensavers."
        )
        pushed_id = None

        entry_after, entries_after = _poll_for_listing(
            live_client, filename, present=False, timeout_s=20
        )
        assert entry_after is None, (
            f"Screensaver {filename!r} still listed after delete + 20s "
            f"(listing has {len(entries_after)} entries)"
        )

    finally:
        # Best-effort cleanup on any failure between push and delete.
        if pushed_id:
            try:
                live_client.screensavers.delete_screensavers([pushed_id])
            except Exception as exc:
                pytest.fail(
                    f"Test failed AND cleanup of pushed screensaver "
                    f"(id={pushed_id}) also failed: {exc}. Manual "
                    f"cleanup required."
                )
