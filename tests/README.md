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

Live tests are skipped by default (see Phase 0 [#3](https://github.com/Great-Sarak/onyx-send2boox/issues/3) for the gating logic, landing next).

## Layout

- `tests/test_*.py` — pytest auto-discovers anything matching this pattern.
- `tests/conftest.py` — shared fixtures (HTTP mock, live-API auth loader). Currently a placeholder; fixtures land in #2 and #3.
- `tests/test_smoke.py` — trivial "is pytest wired up?" canary; safe to delete once real test modules exist.
