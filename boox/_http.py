#  SPDX-License-Identifier: MIT
"""Private HTTP mechanics shared across API-surface modules.

Split from ``boox/__init__.py`` in #45. Holds header construction, method
coercion, the ``requests.request`` call, and the typed-error response
mapping — anything not specific to one API surface. ``BooxClient.api_call``
is a thin wrapper around :func:`api_call` here.

Leading underscore signals "private": consumers go through
``BooxClient.api_call`` (or their own Pattern A subobject's ``self._c``
back-reference), not directly through this module.

``requests.RequestException`` (ConnectionError, Timeout, etc.) deliberately
propagates unchanged — see ``boox/errors.py`` for the rationale.
"""

import json
import logging

import requests

from boox.errors import from_response as _error_from_response


def api_call(cloud, token, api_url, method='GET', headers={}, data={},
             params={}, api='api/1'):
    """Execute a Boox API call and return the parsed JSON body.

    Builds the ``Authorization: Bearer <token>`` header when a token is
    present, coerces the method to POST when ``data`` is non-empty, fires
    the ``requests.request`` call, maps the response to a typed exception
    via ``boox.errors.from_response`` (raises if non-None), then parses
    and returns the JSON body on success.

    Note: ``headers`` is mutated in-place when a token or content-type is
    added — preserved verbatim from the pre-split behavior to keep this
    a no-op restructure (#45). See #51 if a follow-up wants to address it.
    """
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if data:
        headers['Content-Type'] = 'application/json;charset=utf-8'
        method = 'POST'

    r = requests.request(method,
                         f'https://{cloud}/{api}/{api_url}',
                         headers=headers,
                         params=params,
                         data=json.dumps(data))

    exc = _error_from_response(r)
    if exc is not None:
        raise exc

    body = r.json()
    logging.info(json.dumps(body, indent=4))
    logging.info('')

    return body
