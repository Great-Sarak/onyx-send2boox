"""Shared pytest fixtures.

- ``mock_http`` (#2): responses-based intercept layer for the ``requests``
  library. Tests register canned responses; the fixture object also captures
  outgoing requests so tests can assert URL/method/headers/body.
- ``boox_config`` (#4): minimal config-dict factory for unit tests; produces
  the same shape ``boox.Boox`` expects from configparser without needing a
  real config.ini.
- ``unit_client`` (#4): pre-constructed ``Boox(skip_init=True)`` with the
  test token already in place, for tests that exercise ``api_call`` directly.
- ``live_token`` (#3): session-scoped Bearer-JWT loader for
  ``@pytest.mark.live`` tests. Loaded from one of (in priority order): the
  ``BOOX_TOKEN`` env var, the file at ``BOOX_SECRETS_FILE``, or
  ``<repo-root>/secrets/boox.env``.
- ``live_sync_token`` (#3): same three-source pattern for ``BOOX_SYNC_TOKEN``
  (the SyncGatewaySession cookie value). Required for any live test hitting
  ``/neocloud/*`` endpoints since those reject Bearer auth.
- ``pytest_collection_modifyitems`` (#3): skips ``@pytest.mark.live`` tests
  unless ``BOOX_RUN_LIVE_TESTS`` is set in the env.
"""

import os
from pathlib import Path

import pytest
import responses

import boox


# Constants used across unit tests; keeping them here means tests stay
# narrowly focused on behavior, not boilerplate.
TEST_CLOUD = "push.boox.com"
TEST_TOKEN = "test-token-fixture"  # pragma: allowlist secret
TEST_SYNC_TOKEN = "test-sync-token-fixture"  # pragma: allowlist secret
TEST_API_BASE = f"https://{TEST_CLOUD}/api/1"
TEST_NEOCLOUD_BASE = f"https://{TEST_CLOUD}/neocloud"


# --------------------------- HTTP mocking (#2) -----------------------------


@pytest.fixture
def mock_http():
    """Yield a ``responses.RequestsMock`` that intercepts ``requests`` calls.

    Register canned responses for the URLs your code under test will hit, then
    inspect ``.calls`` after the call to assert on captured outgoing requests.

    ``assert_all_requests_are_fired=False`` because tests routinely register
    "you might call this" responses that aren't exercised on every code path;
    a stricter mode can be opted into per-test by passing ``True`` if needed.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


# --------------------------- Unit client config (#4) ----------------------


@pytest.fixture
def boox_config():
    """Minimal config dict matching the shape ``Boox.__init__`` reads.

    Returns the same nested mapping that ``configparser`` exposes when
    parsing a real ``config.ini`` (``cfg['default']['cloud']`` etc), so the
    constructor sees no behavioral difference between this and the on-disk
    file path.
    """
    return {
        "default": {
            "cloud": TEST_CLOUD,
            "token": TEST_TOKEN,
            "sync_token": TEST_SYNC_TOKEN,
        }
    }


@pytest.fixture
def unit_client(boox_config):
    """Pre-constructed ``Boox`` instance ready for unit testing.

    Uses ``skip_init=True`` so the constructor doesn't make network calls;
    the token is then set explicitly so ``api_call`` can authenticate.
    """
    client = boox.Boox(boox_config, skip_init=True)
    client.token = boox_config["default"]["token"]
    return client


# --------------------------- Live-API gating (#3) --------------------------


def pytest_collection_modifyitems(config, items):
    """Skip ``@pytest.mark.live`` tests unless ``BOOX_RUN_LIVE_TESTS`` is set.

    Standard pattern: live tests are collected normally but marked skip at
    collection time. The reason string surfaces in pytest's report so users
    know why they were skipped.
    """
    if os.environ.get("BOOX_RUN_LIVE_TESTS"):
        return
    skip_live = pytest.mark.skip(reason="BOOX_RUN_LIVE_TESTS not set")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def _load_var_from_env_file(path: Path, var_name: str) -> str | None:
    """Parse a simple ``KEY=VALUE`` env file and return ``var_name`` if present.

    Tolerant of leading ``export`` keywords and single- or double-quoted
    values, matching the convention used in our shared ``secrets/boox.env``.
    """
    if not path.is_file():
        return None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line.startswith(var_name):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            return value
    return None


def _load_live_secret(env_var: str) -> str | None:
    """Resolve a live-test secret from the three documented sources.

    Sources tried in order:

    1. ``<env_var>`` env var (preferred for CI / one-off runs).
    2. ``BOOX_SECRETS_FILE`` env var pointing at an env-style file containing
       a ``<env_var>=...`` line (useful for pointing at the shared workspace
       ``secrets/boox.env``).
    3. ``<repo-root>/secrets/boox.env`` (per-repo convention; gitignored).

    Returns None if the secret cannot be located.
    """
    value = os.environ.get(env_var)
    if value:
        return value

    explicit_file = os.environ.get("BOOX_SECRETS_FILE")
    if explicit_file:
        value = _load_var_from_env_file(Path(explicit_file), env_var)
        if value:
            return value

    repo_root = Path(__file__).resolve().parent.parent
    value = _load_var_from_env_file(repo_root / "secrets" / "boox.env", env_var)
    if value:
        return value

    return None


@pytest.fixture(scope="session")
def live_token():
    """Load the Bearer JWT for live tests."""
    token = _load_live_secret("BOOX_TOKEN")
    if not token:
        pytest.skip(
            "No Boox JWT available. Set BOOX_TOKEN, point BOOX_SECRETS_FILE "
            "at an env file with a BOOX_TOKEN= line, or populate "
            "<repo-root>/secrets/boox.env."
        )
    return token


@pytest.fixture(scope="session")
def live_sync_token():
    """Load the SyncGatewaySession cookie value for live tests against
    ``/neocloud/*`` endpoints.

    Same three-source priority as ``live_token`` (env, ``BOOX_SECRETS_FILE``,
    repo ``secrets/boox.env``) but reads ``BOOX_SYNC_TOKEN``. Skips the test
    if missing — sync-touching live tests can't proceed without it because
    ``/neocloud/*`` rejects Bearer auth.
    """
    value = _load_live_secret("BOOX_SYNC_TOKEN")
    if not value:
        pytest.skip(
            "No Sync Gateway session available. Set BOOX_SYNC_TOKEN, point "
            "BOOX_SECRETS_FILE at an env file with a BOOX_SYNC_TOKEN= line, "
            "or populate <repo-root>/secrets/boox.env. Required for live "
            "tests that hit /neocloud/* endpoints."
        )
    return value
