import pytest

from anychain.config import ConfigError, load_config
from tests.conftest import ROOT


def test_loads_both_real_networks(eth_cfg, op_cfg):
    assert eth_cfg.network.chain_id == 1
    assert op_cfg.network.chain_id == 10
    assert eth_cfg.explorer.api_base == "https://eth.blockscout.com/api/v2"


def test_cloudwalk_template_is_valid_yaml_with_placeholders():
    cfg = load_config(str(ROOT / "configs" / "cloudwalk.example.yaml"))
    assert "<" in cfg.explorer.base_url  # still a placeholder, by design


def test_env_var_selects_config(monkeypatch):
    monkeypatch.setenv("ANYCHAIN_CONFIG", str(ROOT / "configs" / "optimism-mainnet.yaml"))
    assert load_config().network.name == "optimism-mainnet"


def test_missing_config_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANYCHAIN_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="No config given"):
        load_config()


def test_invalid_values_are_rejected(tmp_path):
    bad = (ROOT / "configs" / "ethereum-mainnet.yaml").read_text().replace("default_mode: support", "default_mode: hacker")
    path = tmp_path / "bad.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="mode"):
        load_config(str(path))


def test_unknown_abi_source_is_rejected(tmp_path):
    bad = (ROOT / "configs" / "ethereum-mainnet.yaml").read_text().replace("order: [explorer,", "order: [magic, explorer,")
    path = tmp_path / "bad.yaml"
    path.write_text(bad)
    with pytest.raises(ConfigError, match="unknown ABI sources"):
        load_config(str(path))


def test_urls_come_from_config(eth_cfg):
    assert eth_cfg.explorer.tx_url("0xabc") == "https://eth.blockscout.com/tx/0xabc"
