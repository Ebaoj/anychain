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
    text = text.replace('"<RPC_URL>?app=anychain"', "https://rpc.internal.example?app=anychain")
    cfg = load_config(_write(tmp_path, text))
    assert cfg.network.chain_id == 2009 and cfg.network.native_symbol == "CWN"
    assert cfg.rpc.supports_debug_trace and cfg.rpc.url.endswith("?app=anychain")  # Stratus findings


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
    ("chain_type: ethereum", "chain_type: ''", "chain_type"),
])
def test_invalid_values_are_rejected(tmp_path, old, new, message):
    assert old in ETH_YAML
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, ETH_YAML.replace(old, new)))


def test_urls_come_from_config(eth_cfg):
    assert eth_cfg.explorer.tx_url("0xabc") == "https://eth.blockscout.com/tx/0xabc"


def test_address_labels_are_normalized_and_validated(tmp_path):
    text = ETH_YAML.replace("\nabi_strategy:", '\naddress_labels:\n  "0xABCDEF0000000000000000000000000000000001": "Treasury"\nabi_strategy:')
    cfg = load_config(_write(tmp_path, text))
    assert cfg.address_labels == {"0xabcdef0000000000000000000000000000000001": "Treasury"}
    bad = ETH_YAML.replace("\nabi_strategy:", '\naddress_labels:\n  "treasury": "x"\nabi_strategy:')
    with pytest.raises(ConfigError, match="address_labels"):
        load_config(_write(tmp_path, bad))
