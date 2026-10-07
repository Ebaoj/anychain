"""Golden answers: the full evidence bundle of every recorded transaction, frozen.

A refactor must not change any answer. When a change is meant to alter answers,
regenerate on purpose and review the diff:  ANYCHAIN_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
"""
import json
import os
import re

import pytest

from anychain.config import load_config
from tests.conftest import FIXTURES, ROOT, replay_bundle

GOLDEN = ROOT / "tests" / "golden"
CONFIG_BY_PREFIX = {"eth_": "ethereum-mainnet", "op_": "optimism-mainnet", "celo_": "celo-mainnet",
                    "gnosis_": "gnosis-mainnet", "rootstock_": "rootstock-mainnet", "zksync_": "zksync-era"}
TX_KEY = re.compile(r"/transactions/(0x[0-9a-fA-F]{64})$|eth_getTransactionByHash \[\"(0x[0-9a-fA-F]{64})\"\]")


def _case(fixture: str) -> tuple[str, str]:
    """(config name, tx hash) for a fixture, read from the fixture itself."""
    config = next(c for prefix, c in CONFIG_BY_PREFIX.items() if fixture.startswith(prefix))
    records = json.loads((FIXTURES / f"{fixture}.json").read_text())
    for key in records:
        if m := TX_KEY.search(key):
            return config, m.group(1) or m.group(2)
    raise AssertionError(f"no transaction hash in fixture {fixture}")


def _snapshot(fixture: str) -> dict:
    config, tx_hash = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    explorer_host = cfg.explorer.base_url.split("//")[1]
    offline = {explorer_host} if "rpc_only" in fixture else None  # recorded with the explorer down
    return json.loads(replay_bundle(cfg, tx_hash, fixture, offline_hosts=offline).model_dump_json())


FIXTURE_NAMES = sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_answer_matches_golden(fixture):
    path = GOLDEN / f"{fixture}.json"
    actual = _snapshot(fixture)
    if os.environ.get("ANYCHAIN_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        pytest.skip("golden written on purpose; review it with git diff")
    # A missing golden is a failure, never a silent approval of whatever the code says today.
    assert path.exists(), f"no golden answer for {fixture}; create it with ANYCHAIN_UPDATE_GOLDEN=1 and review it"
    assert actual == json.loads(path.read_text()), f"answer changed for {fixture}; see the diff above"
