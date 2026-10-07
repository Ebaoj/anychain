"""Record every real HTTP response needed to explain a transaction.

Usage: uv run python scripts/record_fixture.py <config.yaml> <tx_hash> <fixture_name>
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


def main(config_path: str, tx_hash: str, name: str) -> None:
    cfg = load_config(config_path)
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    bundle = build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
    out = FIXTURES / f"{name}.json"
    transport.save(out)
    print(f"saved {len(transport.records)} responses to {out}; status={bundle.status}, evidence={len(bundle.items)}, gaps={len(bundle.gaps)}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
