"""Record real HTTP traffic to a JSON fixture, and replay it offline in tests.

Fixtures hold only real responses: nothing in tests is invented.
"""
import json
from pathlib import Path

import httpx


def request_key(request: httpx.Request) -> str:
    if request.method == "POST":
        body = json.loads(request.content or b"{}")
        return f"POST {request.url} {body.get('method')} {json.dumps(body.get('params'), sort_keys=True)}"
    return f"GET {request.url}"


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport()
        self.records: dict[str, dict] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = self.inner.handle_request(request)
            response.read()
        except httpx.TimeoutException as exc:
            # Real failures are part of the record too (e.g. a slow explorer endpoint).
            self.records[request_key(request)] = {"timeout": type(exc).__name__}
            raise
        self.records[request_key(request)] = {"status": response.status_code, "body": response.text}
        return response

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.records, indent=1, sort_keys=True))


class ReplayTransport(httpx.BaseTransport):
    """Serves recorded responses. Unknown requests fail like a network outage.

    For tests of bad conditions:
    - `offline_hosts`: every request to these hosts fails to connect.
    - `overrides`: {suffix: response}; a request whose key ends with `suffix` gets
      this response instead, e.g. {"/logs": {"status": 503, "body": ""}}.
      A response {"timeout": True} simulates a timeout.
    `calls` counts requests, so tests can check retries.
    """

    def __init__(
        self, path: Path, offline_hosts: set[str] | None = None, overrides: dict[str, dict] | None = None
    ) -> None:
        self.records = json.loads(Path(path).read_text())
        self.offline_hosts = offline_hosts or set()
        self.overrides = overrides or {}
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        self.calls.append(key)
        if request.url.host in self.offline_hosts:
            raise httpx.ConnectError(f"simulated outage of {request.url.host}", request=request)
        rec = next((r for suffix, r in self.overrides.items() if key.endswith(suffix)), None)
        rec = rec or self.records.get(key)
        if rec is None:
            raise httpx.ConnectError(f"no fixture for {key}", request=request)
        if rec.get("timeout"):
            raise httpx.ReadTimeout(f"recorded timeout for {key}", request=request)
        return httpx.Response(rec["status"], text=rec["body"], request=request)
