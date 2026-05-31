"""Unit test suite — file listing (push/message).

Covers ``Boox.list_files``: request shape (URL, ``where`` filter, Bearer
auth), response parsing, source-type filtering, and the locale-crash fix.

The locale crash (caught here): hrw's original ``list_files`` called
``locale.setlocale(locale.LC_ALL, locale.getlocale()[0])`` which raises
``locale.Error: unsupported locale setting`` whenever ``locale.getlocale()``
returns ``(None, None)`` — common in minimal containers, fresh distros, or
any env without ``LC_ALL`` set. Phase 0 #6 drops the setlocale dance and
uses Python's built-in ``:,`` thousands separator (works in any locale).

Added by #6 (Unit test suite — file listing).
"""

import json
import os
import subprocess
import sys
from urllib.parse import urlparse, parse_qs

import pytest

from .conftest import TEST_API_BASE


# Sample push/message response shape (scrubbed from a real HAR).
_SAMPLE_ENTRY = {
    "data": {
        "args": {
            "_id": "abc123fixtureid",
            "name": "book.pdf",
            "formats": ["pdf"],
            "storage": {
                "pdf": {
                    "oss": {
                        "size": "1234567",
                        "key": "user-uid-fixture/push/uuid.pdf",
                        "bucket": "onyx-cloud-test",
                    }
                }
            },
            "createdAt": 1780270000000,
            "updatedAt": 1780270000000,
        }
    }
}


def _captured_where(call):
    """Extract the JSON-parsed ``where`` query param from a captured call."""
    qs = parse_qs(urlparse(call.request.url).query)
    where_strs = qs.get("where", [])
    assert where_strs, "no where param on outgoing request"
    return json.loads(where_strs[0])


# --------------------------- Request shape ---------------------------------


def test_list_files_request_shape(mock_http, unit_client):
    """Outgoing request hits push/message with Bearer + JSON where filter."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_ENTRY]},
    )

    unit_client.list_files(limit=10, offset=5)

    call = mock_http.calls[0]
    assert call.request.url.startswith(f"{TEST_API_BASE}/push/message")
    assert call.request.headers["Authorization"].startswith("Bearer ")
    where = _captured_where(call)
    assert where == {"limit": 10, "offset": 5, "parent": 0}


def test_list_files_default_params(mock_http, unit_client):
    """Default args produce a sensible (limit=24, offset=0, parent=0) filter."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.list_files()

    where = _captured_where(mock_http.calls[0])
    assert where == {"limit": 24, "offset": 0, "parent": 0}


def test_list_files_screensaver_source_type(mock_http, unit_client):
    """Screensavers use ``source_type=100`` (Phase 0 #6 widening of API)."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.list_files(source_type=100)

    where = _captured_where(mock_http.calls[0])
    assert where.get("sourceType") == 100


def test_list_files_custom_parent(mock_http, unit_client):
    """Parent filter — used when listing a folder's contents."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    unit_client.list_files(parent="folder-id-fixture")

    where = _captured_where(mock_http.calls[0])
    assert where["parent"] == "folder-id-fixture"


# --------------------------- Response parsing ------------------------------


def test_list_files_returns_parsed_entries(mock_http, unit_client):
    """list_files returns the raw list (so callers can iterate / assert)."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_ENTRY]},
    )

    result = unit_client.list_files()

    assert isinstance(result, list)
    assert len(result) == 1
    args = result[0]["data"]["args"]
    assert args["_id"] == "abc123fixtureid"
    assert args["name"] == "book.pdf"
    assert args["formats"] == ["pdf"]


def test_list_files_empty_response(mock_http, unit_client, capsys):
    """Empty listing renders the header but no rows."""
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": []},
    )

    result = unit_client.list_files()

    assert result == []
    captured = capsys.readouterr()
    assert "ID" in captured.out  # header still printed


def test_list_files_renders_size_with_thousands_separator(
    mock_http, unit_client, capsys
):
    """Size column uses comma thousands separator (locale-independent).

    Catches the original ``:>10n`` formatter which depended on
    ``locale.setlocale`` being valid. The fix uses ``:,`` which works in
    any locale and produces ``1,234,567`` for a 1.2 MB file.
    """
    mock_http.get(
        f"{TEST_API_BASE}/push/message",
        json={"result_code": 0, "list": [_SAMPLE_ENTRY]},
    )

    unit_client.list_files()

    captured = capsys.readouterr()
    assert "1,234,567" in captured.out


# --------------------------- Locale crash -----------------------------------


def test_list_files_runs_without_lc_all(tmp_path):
    """list_files completes in an env with LC_ALL and LANG unset.

    Reproduces the original 2026-05-31 crash environment as a subprocess
    so we can clear locale env vars cleanly without affecting the parent
    test process.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = tmp_path / "locale_crash_probe.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "import boox\n"
        "import responses\n"
        "rsps = responses.RequestsMock(assert_all_requests_are_fired=False)\n"
        "rsps.start()\n"
        "rsps.get(\n"
        "    'https://push.boox.com/api/1/push/message',\n"
        "    json={'result_code': 0, 'list': []},\n"
        ")\n"
        "config = {'default': {'cloud': 'push.boox.com', 'token': 't'}}\n"
        "client = boox.Boox(config, skip_init=True)\n"
        "client.token = 't'\n"
        "client.list_files()\n"
        "rsps.stop()\n"
        "print('OK')\n"
    )

    # Strip LC_ALL/LANG/LC_* — but keep PATH and PYTHONPATH
    env = {
        k: v for k, v in os.environ.items()
        if not (k.startswith("LC_") or k == "LANG" or k == "LANGUAGE")
    }
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, (
        f"list_files crashed under LC_ALL-stripped env.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
