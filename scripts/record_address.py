"""Record the explorer's real answer for an address's latest transactions (triage, PHASE4 R1).

Usage: uv run python scripts/record_address.py <config.yaml> <address> <fixture_name>
"""
import sys
from pathlib import Path

import httpx

from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import USER_AGENT
from anychain.collectors.replay import RecordingTransport
from anychain.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures_triage"


def main(config_path: str, address: str, name: str) -> None:
    cfg = load_config(config_path)
    transport = RecordingTransport()
    client = httpx.Client(transport=transport, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20)
    rows = ExplorerClient(cfg.explorer, client).address_transactions(address)
    out = FIXTURES / f"{name}.json"
    transport.save(out)
    print(f"saved {len(transport.records)} responses to {out}; {len(rows)} transactions")


if __name__ == "__main__":
    main(*sys.argv[1:4])
