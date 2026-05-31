# tests/

Pytest-based test suite for `onyx-send2boox`. Scaffolded in Phase 0 [#1](https://github.com/Great-Sarak/onyx-send2boox/issues/1); filled in across the rest of Phase 0.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Run

```bash
pytest                 # unit tests only
pytest -m live         # live-API integration tests (requires BOOX_RUN_LIVE_TESTS=1 + valid token)
pytest -m "not live"   # explicit unit-only when you have the env set
```

Live tests are skipped by default — landing logic in Phase 0 [#3](https://github.com/Great-Sarak/onyx-send2boox/issues/3). Tokens load from:

1. `BOOX_TOKEN` env var (preferred for CI / one-off).
2. `BOOX_SECRETS_FILE` env var pointing at an env-style file with a `BOOX_TOKEN=...` line (useful for the shared workspace `secrets/boox.env`).
3. `<repo-root>/secrets/boox.env` (per-repo, gitignored).

Example:
```bash
BOOX_RUN_LIVE_TESTS=1 BOOX_SECRETS_FILE=/path/to/secrets/boox.env pytest -m live
```

## Layout

- `tests/test_*.py` — pytest auto-discovers anything matching this pattern.
- `tests/conftest.py` — shared fixtures.
  - `mock_http` (#2) — `responses`-based intercept for the `requests` library; tests register canned responses and inspect captured outgoing requests.
  - Live-API auth fixture lands in #3.
- `tests/test_smoke.py` — trivial "is pytest wired up?" canary; safe to delete once real test modules exist.
- `tests/test_mock_http_fixture.py` — worked examples for the `mock_http` fixture; serves as living documentation for the patterns #4–#8 use.

## The `mock_http` fixture

Standard pattern:

```python
def test_something(mock_http):
    # Register a canned response.
    mock_http.get(
        "https://push.boox.com/api/1/users/me",
        json={"result_code": 0, "data": {...}},
        status=200,
    )
    # Call your code under test.
    result = client.api_call("users/me")
    # Inspect captured outgoing request.
    req = mock_http.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-token"
```

See `tests/test_mock_http_fixture.py` for three worked examples (pure `requests`, `Boox.api_call` with `skip_init=True`, and asserting on POST body).
