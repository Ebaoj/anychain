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
        response = self.inner.handle_request(request)
        response.read()
        self.records[request_key(request)] = {"status": response.status_code, "body": response.text}
        return response

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.records, indent=1, sort_keys=True))


class ReplayTransport(httpx.BaseTransport):
    """Serves recorded responses. Unknown requests fail like a network outage."""

    def __init__(self, path: Path, offline_hosts: set[str] | None = None) -> None:
        self.records = json.loads(Path(path).read_text())
        self.offline_hosts = offline_hosts or set()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.offline_hosts:
            raise httpx.ConnectError(f"simulated outage of {request.url.host}", request=request)
        rec = self.records.get(request_key(request))
        if rec is None:
            raise httpx.ConnectError(f"no fixture for {request_key(request)}", request=request)
        return httpx.Response(rec["status"], text=rec["body"], request=request)
