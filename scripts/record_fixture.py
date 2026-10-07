"""Record every real HTTP response needed to explain a transaction.

Usage: uv run python scripts/record_fixture.py <config.yaml> <tx_hash> <fixture_name> [--explorer-down]

--explorer-down records the RPC-only path (the explorer is made unreachable), so the
extra RPC calls that path makes are recorded from the real node too.
"""
import sys
from pathlib import Path

import httpx

from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import USER_AGENT
from anychain.collectors.replay import RecordingTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("explorer made unreachable for this recording", request=request)


def main(config_path: str, tx_hash: str, name: str, explorer_down: bool = False) -> None:
    cfg = load_config(config_path)
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    explorer_client = httpx.Client(transport=httpx.MockTransport(_unreachable)) if explorer_down else client
    bundle = build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, explorer_client), RpcClient(cfg.rpc, client))
    out = FIXTURES / f"{name}.json"
    transport.save(out)
    print(f"saved {len(transport.records)} responses to {out}; status={bundle.status}, evidence={len(bundle.items)}, gaps={len(bundle.gaps)}")


if __name__ == "__main__":
    main(*sys.argv[1:4], explorer_down="--explorer-down" in sys.argv)
