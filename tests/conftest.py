from pathlib import Path

import httpx
import pytest

from anychain.bundle import build_bundle
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.replay import ReplayTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

USDC_TX = "0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952"
SWAP_TX = "0x08b39d3ae20b5c09294209ad4c0bf8e6d14bf660f81ffa6696c455911aa46c6a"
OP_TX = "0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921"


@pytest.fixture
def eth_cfg():
    return load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))


@pytest.fixture
def op_cfg():
    return load_config(str(ROOT / "configs" / "optimism-mainnet.yaml"))


def replay_bundle(cfg, tx_hash, fixture, offline_hosts=None):
    """Build a bundle from recorded responses; `offline_hosts` simulates outages."""
    transport = ReplayTransport(FIXTURES / f"{fixture}.json", offline_hosts)
    client = httpx.Client(transport=transport)
    return build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
