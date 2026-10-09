"""Add to an existing recording the real answers it lacks for one kind of request, fetched live now, keeping every
answer already recorded (PHASE4 T2: the sender's transaction lists the timeline reads, which recordings made before
it did not have).

Usage: uv run python scripts/complete_fixture.py <config.yaml> <fixture_name> [<path part>]   (default "/addresses/")
"""
import json
import re
import sys
from pathlib import Path

import httpx

from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import USER_AGENT
from anychain.collectors.replay import ReplayTransport, request_key
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


class CompletingTransport(ReplayTransport):
    """Replays the recording; a request it lacks whose URL contains `part` is fetched live and recorded."""

    def __init__(self, path: Path, part: str) -> None:
        super().__init__(path)
        self.part, self.live, self.added = part, httpx.HTTPTransport(), {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        if key not in self.records and self.part in str(request.url):
            response = self.live.handle_request(request)
            response.read()
            self.records[key] = self.added[key] = {"status": response.status_code, "body": response.text}
        return super().handle_request(request)


def main(config_path: str, name: str, part: str = "/addresses/") -> None:
    cfg = load_config(config_path)
    path = FIXTURES / f"{name}.json"
    records = json.loads(path.read_text())
    golden = FIXTURES.parent / "golden" / f"{name}.json"  # the transaction the recording is about
    tx_hash = json.loads(golden.read_text())["tx_hash"] if golden.exists() else next(
        m.group(1) for k in records if (m := re.search(r"/transactions/(0x[0-9a-fA-F]{64})$", k)))
    transport = CompletingTransport(path, part)
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
    path.write_text(json.dumps({**records, **transport.added}, indent=1, sort_keys=True))
    print(f"{name}: added {len(transport.added)} real answers ({part})")


if __name__ == "__main__":
    main(*sys.argv[1:4])
