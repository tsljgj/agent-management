"""Tiny stdlib HTTP helper so the tool has zero runtime dependencies."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from .models import ProviderError

TIMEOUT = 20


class HTTPStatusError(ProviderError):
    def __init__(self, status: int, body: str, url: str, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")


def request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    form_body: dict | None = None,
) -> dict:
    headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        raise HTTPStatusError(e.code, body, url, dict(e.headers or {})) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network error for {url}: {e.reason}") from None
    except (OSError, http.client.HTTPException) as e:
        # urllib wraps only the connect: a server that is slow to answer, or a dropped connection,
        # surfaces as a bare TimeoutError / ConnectionResetError / RemoteDisconnected / IncompleteRead.
        raise ProviderError(f"network error for {url}: {str(e) or type(e).__name__}") from None
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise ProviderError(f"non-JSON response from {url}: {raw[:200]}") from None
