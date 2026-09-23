"""
_http.py -- the only place this package talks to the network.

`requests` when it's there, `urllib` when it isn't. That sounds like
pedantry until you try to run a simulation in the Jugnu account: it has a
bare system Python with no packages, can't read your home folder, and is
exactly where you want long sweeps to run. One small shim makes the whole
trading package portable to any Python 3 on the machine.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Jugnu/1.0"

try:                                    # pragma: no cover - depends on the interpreter
    import requests as _requests
except ImportError:                     # pragma: no cover
    _requests = None


class HttpError(RuntimeError):
    pass


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: float = 20.0):
    """A GET that returns parsed JSON, or raises HttpError with something
    readable."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    if _requests is not None:
        try:
            response = _requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:                      # noqa: BLE001 - one error type out
            raise HttpError(str(e)) from e
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310
            return _json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise HttpError(str(e)) from e


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30.0):
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})}
    body = _json.dumps(payload).encode()
    if _requests is not None:
        try:
            response = _requests.post(url, data=body, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:                      # noqa: BLE001
            raise HttpError(str(e)) from e
    try:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310
            return _json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise HttpError(str(e)) from e
