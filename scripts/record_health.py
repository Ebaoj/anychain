"""Record the real answers of the /health probes (the explorer's /stats and the node's eth_chainId).

Usage: uv run python scripts/record_health.py <config.yaml> <fixture_name>
"""
import sys
from pathlib import Path

import httpx

from anychain.api import probe_explorer, probe_node
from anychain.collectors.http import USER_AGENT
from anychain.collectors.replay import RecordingTransport
from anychain.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures_health"  # not transactions: kept apart


def main(config_path: str, name: str) -> None:
    cfg = load_config(config_path)
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
    print(probe_explorer(cfg, client), probe_node(cfg, client))
    transport.save(FIXTURES / f"{name}.json")


if __name__ == "__main__":
    main(*sys.argv[1:3])
