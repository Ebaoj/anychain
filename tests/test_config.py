import pytest

from anychain.config import ConfigError, load_config
from tests.conftest import ROOT

ETH_YAML = (ROOT / "configs" / "ethereum-mainnet.yaml").read_text()


def _write(tmp_path, text):
    path = tmp_path / "cfg.yaml"
    path.write_text(text)
    return str(path)


def test_loads_both_real_networks(eth_cfg, op_cfg):
    assert eth_cfg.network.chain_id == 1
    assert op_cfg.network.chain_id == 10
    assert eth_cfg.explorer.api_base == "https://eth.blockscout.com/api/v2"


def test_cloudwalk_template_refuses_to_run_until_filled():
    with pytest.raises(ConfigError, match="EXPLORER_BASE_URL"):
        load_config(str(ROOT / "configs" / "cloudwalk.example.yaml"))


def test_cloudwalk_template_works_once_urls_are_filled(tmp_path):
    text = (ROOT / "configs" / "cloudwalk.example.yaml").read_text()
    text = text.replace('"<EXPLORER_BASE_URL>"', "https://explorer.internal.example")
    text = text.replace('"<RPC_URL>"', "https://rpc.internal.example")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.network.chain_id == 2009 and cfg.network.native_symbol == "CWN"


def test_env_var_selects_config(monkeypatch):
    monkeypatch.setenv("ANYCHAIN_CONFIG", str(ROOT / "configs" / "optimism-mainnet.yaml"))
    assert load_config().network.name == "optimism-mainnet"


def test_missing_config_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANYCHAIN_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="No config given"):
        load_config()


@pytest.mark.parametrize("old,new,message", [
    ("default_mode: support", "default_mode: hacker", "mode"),
    ("order: [explorer,", "order: [magic, explorer,", "unknown ABI sources"),
    ("url: https://ethereum-rpc.publicnode.com", "url: ethereum-rpc.publicnode.com", "http"),
    ("time_budget_s: 30", "time_budget_s: 0", "time_budget_s"),
])
def test_invalid_values_are_rejected(tmp_path, old, new, message):
    assert old in ETH_YAML
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, ETH_YAML.replace(old, new)))


def test_urls_come_from_config(eth_cfg):
    assert eth_cfg.explorer.tx_url("0xabc") == "https://eth.blockscout.com/tx/0xabc"
