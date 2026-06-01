"""Baseline live smoke suite — push → list → delete round-trip.

Drives the full BooxDrop happy-path against the live Boox cloud using a
small synthetic PDF; cleans up afterward regardless of failure. This is
the regression net that lets us refactor in Phase 1+ with confidence.

Requires both:
- BOOX_TOKEN (Bearer JWT for /api/1/*)
- BOOX_SYNC_TOKEN (SyncGatewaySession cookie for /neocloud/*)

Skipped by default; run with:

    BOOX_RUN_LIVE_TESTS=1 \\
    BOOX_SECRETS_FILE=/path/to/secrets/boox.env \\
    pytest -m live tests/test_live_smoke.py -v

Added by #9 (Baseline live smoke suite).
"""

import datetime
import time
import uuid

import pytest

import boox


# Minimal valid-ish PDF body — a header + EOF is enough for the cloud to
# accept the upload; the reader may render it as empty but won't reject.
_MINIMAL_PDF_BYTES = b"%PDF-1.4\n%minimal-pdf-fixture\n%%EOF\n"


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


@pytest.fixture
def tracked_smoke_pdf(tmp_path):
    """Write a small synthetic PDF with a distinctive name for traceability."""
    name = f"pytest-smoke-{uuid.uuid4().hex[:12]}.pdf"
    path = tmp_path / name
    path.write_bytes(_MINIMAL_PDF_BYTES)
    return path


def _find_by_name(files, name):
    """Return the listing entry matching ``name`` or None."""
    for entry in files:
        args = entry.get("data", {}).get("args", {})
        if args.get("name") == name:
            return entry
    return None


def _poll_for_listing(client, filename, *, present, timeout_s=20, interval_s=2):
    """Re-list every ``interval_s`` until ``filename`` matches ``present``.

    Cloud-side indexing isn't instant — observed 2026-05-31 to take a few
    seconds after push. Polling avoids flakes from a fixed sleep being
    just slightly too short.
    """
    deadline = time.monotonic() + timeout_s
    last_files = []
    while time.monotonic() < deadline:
        last_files = client.list_files(limit=50)
        entry = _find_by_name(last_files, filename)
        if (entry is not None) == present:
            return entry, last_files
        time.sleep(interval_s)
    return _find_by_name(last_files, filename), last_files


@pytest.mark.live
def test_live_push_list_delete_roundtrip(live_client, tracked_smoke_pdf):
    """End-to-end: push a file, confirm it lists with valid timestamps,
    delete it, confirm it's gone. Cleans up on any failure path."""
    filename = tracked_smoke_pdf.name
    pushed_id = None

    try:
        # 1) Push.
        live_client.send_file(str(tracked_smoke_pdf))

        # 2) Poll for the file to appear in the listing. Cloud-side indexing
        # isn't instant — observed to take 2–10s.
        entry, files = _poll_for_listing(
            live_client, filename, present=True, timeout_s=20
        )
        assert entry is not None, (
            f"Just-pushed file {filename!r} not in listing of {len(files)} "
            f"entries after 20s. This is the NaN-timestamp regression "
            f"(see #5) or a much longer indexing delay than observed."
        )

        args = entry["data"]["args"]
        pushed_id = args["_id"]

        # 3) Verify metadata shape: timestamps are present + parseable +
        # close to "now". Boox normalizes server-side: we send epoch-ms ints
        # in the bulk_docs body but the push/message listing returns them
        # as ISO-8601 strings (observed 2026-06-01). Accept either form.
        now_ms = time.time() * 1000
        for ts_field in ("createdAt", "updatedAt"):
            ts = args.get(ts_field)
            assert ts, f"{ts_field} missing on pushed file"
            if isinstance(ts, int):
                ts_ms = ts
            elif isinstance(ts, str):
                # ISO 8601 with trailing Z. Strip Z, parse, convert to ms.
                parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts_ms = parsed.timestamp() * 1000
            else:
                pytest.fail(
                    f"{ts_field} has unexpected type {type(ts).__name__}: {ts!r}"
                )
            assert abs(now_ms - ts_ms) < 5 * 60 * 1000, (
                f"{ts_field}={ts!r} is suspiciously far from now ({now_ms}) — "
                f"timestamp regression?"
            )

        # 4) resourceType — fixture is .pdf so the listing should report it.
        assert "pdf" in args.get("formats", []), (
            f"resourceType regression: formats={args.get('formats')!r}"
        )

        # 5) Delete.
        del_resp = live_client.delete_files([pushed_id])
        assert del_resp.get("result_code") == 0, (
            f"delete_files returned non-zero: {del_resp}"
        )

        # Mark as cleaned up so the finally block doesn't re-delete.
        pushed_id = None

        # 6) Poll for it to disappear from the listing.
        entry_after, files_after = _poll_for_listing(
            live_client, filename, present=False, timeout_s=20
        )
        assert entry_after is None, (
            f"File {filename!r} still listed after delete + 20s wait "
            f"(listing has {len(files_after)} entries)"
        )

    finally:
        # Best-effort cleanup on any failure between push and delete.
        if pushed_id:
            try:
                live_client.delete_files([pushed_id])
            except Exception as exc:
                pytest.fail(
                    f"Test failed AND cleanup of pushed file (id={pushed_id}) "
                    f"also failed: {exc}. Manual cleanup required."
                )
