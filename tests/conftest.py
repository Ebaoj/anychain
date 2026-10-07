"""Shared test helpers. All data comes from real recorded responses in tests/fixtures/."""
import json
from pathlib import Path

import httpx
import pytest

from anychain.bundle import build_bundle
from anychain.collectors import http as http_module
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.replay import ReplayTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# Real transactions, recorded with scripts/record_fixture.py
USDC_TX = "0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952"
SWAP_TX = "0x08b39d3ae20b5c09294209ad4c0bf8e6d14bf660f81ffa6696c455911aa46c6a"
OP_TX = "0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921"
FAILED_TX = "0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9"
CREATION_TX = "0xc31d7e7e85cab1d38ce1b8ac17e821ccd47dbde00f9d57f2bd8613bff9428396"
ERC721_TX = "0x5111a725276204966e7476adc1cebc04f6dbdb79dbf34c0e82994bbe7277c7ab"
ERC1155_TX = "0x2017b9004ee61d547e8d6d480a4ad0cc4d6658dfeba38268b24e63e77014cdaa"
PENDING_TX = "0x44551be6bbd24fba2308724bebe6f10daf952d3b6d249603a56b16a719bc78d0"

EXPLORER_HOST = "eth.blockscout.com"
RPC_HOST = "ethereum-rpc.publicnode.com"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Retries wait between attempts; tests should not."""
    monkeypatch.setattr(http_module.time, "sleep", lambda _s: None)


@pytest.fixture
def eth_cfg():
    return load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))


@pytest.fixture
def op_cfg():
    return load_config(str(ROOT / "configs" / "optimism-mainnet.yaml"))


def make_transport(fixture, offline_hosts=None, overrides=None) -> ReplayTransport:
    return ReplayTransport(FIXTURES / f"{fixture}.json", offline_hosts, overrides)


def replay_bundle(cfg, tx_hash, fixture, offline_hosts=None, overrides=None, transport=None):
    """Build a bundle from recorded responses, optionally simulating outages or bad payloads."""
    transport = transport or make_transport(fixture, offline_hosts, overrides)
    client = httpx.Client(transport=transport)
    return build_bundle(tx_hash, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))


def recorded_body(fixture: str, key_suffix: str) -> dict:
    """The recorded JSON body whose request key ends with `key_suffix`."""
    records = json.loads((FIXTURES / f"{fixture}.json").read_text())
    return json.loads(next(v["body"] for k, v in records.items() if k.endswith(key_suffix)))


def mutated(fixture: str, key_suffix: str, change) -> dict:
    """An override serving a real recorded body after `change(body)` edits it.

    Used to simulate malformed payloads; the starting point is always real data.
    """
    body = recorded_body(fixture, key_suffix)
    change(body)
    return {key_suffix: {"status": 200, "body": json.dumps(body)}}
