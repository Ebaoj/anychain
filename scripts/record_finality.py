"""Add the node's real finality answers (finalized block and latest block) to an existing fixture.

Usage: uv run python scripts/record_finality.py <config.yaml> <fixture_name>

The cache (PHASE3 T0, D44) asks the node whether a transaction's block is final before keeping its
answer; tests replay these real answers, recorded the moment this script runs.
"""
import json
import sys
from pathlib import Path

import httpx

from anychain.collectors.http import USER_AGENT, CollectorError
from anychain.collectors.replay import RecordingTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main(config_path: str, name: str) -> None:
    cfg = load_config(config_path)
    transport = RecordingTransport()
    rpc = RpcClient(cfg.rpc, httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, timeout=20))
    try:
        finalized = rpc.finalized_block()
    except CollectorError as exc:
        finalized = f"failed: {exc}"
    latest = rpc.block_number()
    path = FIXTURES / f"{name}.json"
    records = json.loads(path.read_text())
    records.update(transport.records)
    path.write_text(json.dumps(records, indent=1, sort_keys=True))
    print(f"{name}: finalized={finalized} latest={latest} ({len(transport.records)} responses added)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
