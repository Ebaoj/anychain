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
LIDO_TX = "0x1a6e713214c8d5910c1ea8f8c9b61087cdeaff042c132a03222107b9e1abb36b"  # payable proxy
REVOKE_7702_TX = "0x86b6c6345d9fe5c6145218b5e926be2f65ffa4234b440cdf82cacadecfc4d1a9"
EXECUTE_7702_TX = "0xeaf8e978ba0ea10789ff298ad88e830528321ea0cabbce9755de3fd4c04c1305"
DATA_TO_EOA_TX = "0x863b5a09226c4304b44b1583a84dcc7098b9ff6bad34dde5eb49c8ac6c5a9a44"
SAFE_DEPLOY_TX = "0x31c661a8df887c34b19812ba41074a62644cb4ed498c7b7821ae435000175ebc"
MAKER_TX = "0xaea61c0672217a85a63fbdddc14714b39ffa64baf25a526cf5a9ba74841484d7"  # anonymous events
SET_THEN_REVOKED_7702_TX = "0xf5199e66f52f95ba3ed23340c81ef924296c684f9e0f8bb69975ab25ace3ff8c"
INVALID_NONCE_7702_TX = "0x26118f7faf1f0c16a7e695d3a922811a9aa39ce2a3ffe57c0503012632d19960"

# Every recorded Ethereum fixture, for checks that must hold on all of them.
ETH_FIXTURES = {
    "eth_usdc_transfer": USDC_TX, "eth_uniswap_v2_swap": SWAP_TX, "eth_failed_unverified_bot": FAILED_TX,
    "eth_contract_creation": CREATION_TX, "eth_erc721_transfer": ERC721_TX, "eth_erc1155_transfer": ERC1155_TX,
    "eth_pending_swap": PENDING_TX, "eth_lido_submit": LIDO_TX, "eth_7702_revoke": REVOKE_7702_TX,
    "eth_7702_execute": EXECUTE_7702_TX, "eth_data_to_eoa": DATA_TO_EOA_TX, "eth_safe_deploy": SAFE_DEPLOY_TX,
    "eth_maker_vat": MAKER_TX, "eth_7702_set_then_revoked": SET_THEN_REVOKED_7702_TX,
    "eth_7702_invalid_nonce": INVALID_NONCE_7702_TX,
    "eth_dsproxy_recipe": "0x92f208d329d76c8e557a0f64c7527efca13ef7748f9cc56ebb498f90f25e172a",
}

EXPLORER_HOST = "eth.blockscout.com"
RPC_HOST = "ethereum-rpc.publicnode.com"


@pytest.fixture(autouse=True)
def llm_settings_file(monkeypatch, tmp_path):
    """Tests never read or write the reader's saved model and key (D61): each test gets its own empty file."""
    monkeypatch.setenv("ANYCHAIN_LLM_SETTINGS", str(tmp_path / "llm.json"))
    monkeypatch.setenv("ANYCHAIN_NETWORKS_DIR", str(tmp_path / "networks"))  # never the reader's own networks (D65)
    from anychain import llm_settings

    def offline(request):
        raise httpx.ConnectError("tests never call a model provider", request=request)
    monkeypatch.setattr(llm_settings, "_client", lambda: httpx.Client(transport=httpx.MockTransport(offline)))


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Retries wait between attempts; tests should not."""
    monkeypatch.setattr(http_module.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def event_log(monkeypatch, tmp_path):
    """Tests never write to the real event log; each test gets its own, and can read it."""
    from anychain import cli
    from anychain.events import SqliteEventLog
    log = SqliteEventLog(tmp_path / "runs.db")
    monkeypatch.setattr(cli, "event_log_for", lambda _sink, _path: log)
    return log


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


REPO_CACHE = ROOT / "tests" / "fixtures_repos" / "repos"


@pytest.fixture(autouse=True)
def answer_cache(monkeypatch, tmp_path):
    """Tests never touch the real cache or ask a live node whether a block is final: each test gets its own
    cache file, and no node (nothing is kept) unless the test gives one."""
    from anychain import cli
    from anychain.cache import BundleCache
    # created only when a command asks for it: opening SQLite in each of ~800 tests doubled the suite's time
    monkeypatch.setattr(cli, "cache_for", lambda _cfg: BundleCache(tmp_path / "cache.db"))
    monkeypatch.setattr(cli, "finality_rpc_for", lambda _cfg: None)


@pytest.fixture(autouse=True)
def repo_cache(monkeypatch):
    """Tests read configured repos from real snapshots in tests/fixtures_repos, never the local cache."""
    from anychain.collectors import repo as repo_module
    monkeypatch.setattr(repo_module.RepoCache, "__init__", lambda self, _cache_dir: setattr(self, "root", REPO_CACHE))

SIGNATURES = ROOT / "tests" / "fixtures_signatures"


@pytest.fixture(autouse=True)
def signature_db(monkeypatch):
    """Tests read the public signature database from real recorded answers only (offline)."""
    from anychain.collectors import signatures as sig_module
    real_init = sig_module.SignatureDb.__init__

    def offline(self, cfg, _cache_dir, *args, **kwargs):
        kwargs["offline"] = True  # the config's own enabled flag still decides
        real_init(self, cfg, SIGNATURES, *args, **kwargs)
    monkeypatch.setattr(sig_module.SignatureDb, "__init__", offline)
