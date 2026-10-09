"""D64: the page switches the network; each network is still only its config file."""
import httpx
from fastapi.testclient import TestClient

from anychain.api import create_app
from anychain.config import load_config
from anychain.networks import NetworkSwitcher, available
from tests.conftest import ROOT
from tests.test_api import LOCAL, make_transport

H = {"X-AnyChain-Request": "1"}


def _switcher(event_log):
    paths = available(ROOT / "configs")
    eth = load_config(str(paths["ethereum-mainnet"]))
    return NetworkSwitcher(paths, eth, lambda cfg, sw: create_app(
        cfg, log=event_log, store=None, finality=lambda: None, networks=sw,
        probe_client=httpx.Client(transport=make_transport("health_ethereum"))))


def test_every_shipped_network_is_offered_and_templates_are_not():
    names = set(available(ROOT / "configs"))
    assert {"ethereum-mainnet", "optimism-mainnet", "celo-mainnet", "gnosis-mainnet", "rootstock-mainnet",
            "zksync-era", "anychain-devnet"} <= names
    assert not any("cloudwalk" in n for n in names)  # the template has placeholders


def test_the_page_switches_the_network(event_log):
    c = TestClient(_switcher(event_log), base_url=LOCAL)
    before = c.get("/settings/network").json()
    assert before["active"] == "ethereum-mainnet" and len(before["examples"]) == 5
    assert c.post("/settings/network", json={"name": "celo-mainnet"}).status_code == 403  # not from the page
    assert c.post("/settings/network", json={"name": "celo-mainnet"}, headers=H).json() == {"active": "celo-mainnet"}
    after = c.get("/settings/network").json()
    assert after["active"] == "celo-mainnet" and [n["active"] for n in after["networks"] if n["name"] == "celo-mainnet"] == [True]
    assert c.post("/settings/network", json={"name": "nope"}, headers=H).status_code == 422


def test_the_answers_language_is_saved_and_used(event_log):
    from anychain import llm_settings
    c = TestClient(_switcher(event_log), base_url=LOCAL)
    assert c.post("/settings/answers", json={"language": "en"}, headers=H).json() == {"language": "en"}
    assert llm_settings.language("pt-BR") == "en"
    assert c.post("/settings/answers", json={"language": "klingon"}, headers=H).status_code == 422


# ---- D65: a network added on the page ----

DRAFT = {"name": "my-ethereum", "chain_id": 1, "native_symbol": "ETH", "native_decimals": 18, "chain_type": "ethereum",
         "explorer_url": "https://eth.blockscout.com", "rpc_url": "https://ethereum-rpc.publicnode.com"}


def test_a_network_is_checked_saved_outside_the_repo_and_used(event_log, tmp_path, monkeypatch):
    import os
    import stat
    monkeypatch.setenv("ANYCHAIN_NETWORKS_DIR", str(tmp_path / "nets"))
    c = TestClient(_switcher(event_log), base_url=LOCAL)
    check = c.post("/settings/network/check", json=DRAFT, headers=H).json()
    assert check["explorer"]["status"] == "ok" and check["node"]["status"] == "ok"  # recorded real answers
    saved = c.post("/settings/network/save", json=DRAFT, headers=H)
    assert saved.status_code == 200 and saved.json()["active"] == "my-ethereum"
    path = tmp_path / "nets" / "my-ethereum.yaml"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_config(str(path)).rpc.url == DRAFT["rpc_url"]
    assert "my-ethereum" in [n["name"] for n in c.get("/settings/network").json()["networks"]]
    assert "my-ethereum" in available(ROOT / "configs")  # found again after a restart


def test_a_node_on_another_chain_is_refused(event_log, tmp_path, monkeypatch):
    monkeypatch.setenv("ANYCHAIN_NETWORKS_DIR", str(tmp_path / "nets"))
    c = TestClient(_switcher(event_log), base_url=LOCAL)
    r = c.post("/settings/network/save", json={**DRAFT, "chain_id": 10}, headers=H)
    assert r.status_code == 422 and "chain 1" in r.text and not (tmp_path / "nets" / "my-ethereum.yaml").exists()


def test_bad_drafts_and_other_sites_are_refused(event_log, tmp_path, monkeypatch):
    monkeypatch.setenv("ANYCHAIN_NETWORKS_DIR", str(tmp_path / "nets"))
    c = TestClient(_switcher(event_log), base_url=LOCAL)
    assert c.post("/settings/network/save", json=DRAFT).status_code == 403
    assert c.post("/settings/network/save", json={**DRAFT, "name": "../etc"}, headers=H).status_code == 422
    assert c.post("/settings/network/save", json={**DRAFT, "explorer_url": "javascript:alert(1)"}, headers=H).status_code == 422
    assert c.post("/settings/network/save", json={**DRAFT, "name": "ethereum-mainnet"}, headers=H).status_code == 422
    assert c.get("/settings/network/draft?name=ethereum-mainnet").json()["rpc_url"] == ""  # never sent back
