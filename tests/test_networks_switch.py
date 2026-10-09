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
