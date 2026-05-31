"""Shared pytest fixtures.

- ``mock_http`` (#2): responses-based intercept layer for the ``requests``
  library. Tests register canned responses; the fixture object also captures
  outgoing requests so tests can assert URL/method/headers/body.
- Live-API gating fixture lands in #3.
"""

import pytest
import responses


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
