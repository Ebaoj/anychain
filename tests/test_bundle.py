import pytest

from anychain.bundle import _amount, build_bundle
from tests.conftest import OP_TX, SWAP_TX, USDC_TX, replay_bundle


def test_amount_is_exact():
    assert _amount("69348400", 6) == "69.3484"
    assert _amount(10**18, 18) == "1"
    assert _amount(1, 18) == "0.000000000000000001"


def test_usdc_transfer_facts(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    assert b.status == "success"
    texts = " ".join(e.text for e in b.items)
    assert "69.3484 USDC" in texts
    assert "transfer(address,uint256)" in texts
    assert "proxy -> FiatTokenV2_2" in texts
    assert b.gaps == []


def test_every_fact_has_a_clickable_or_described_source(eth_cfg):
    b = replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap")
    for ev in b.items:
        assert ev.sources and all(s.url or s.detail for s in ev.sources)
    assert [e.id for e in b.items] == [f"E{i}" for i in range(1, len(b.items) + 1)]


def test_swap_has_transfers_events_and_cross_check(eth_cfg):
    b = replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap")
    kinds = {e.kind for e in b.items}
    assert {"overview", "fee", "call", "token_transfer", "internal_call", "event", "cross_check"} <= kinds
    events = [e.data.get("event") for e in b.items if e.kind == "event"]
    assert "Swap" in events
    assert any("BUNKER" in e.text for e in b.items if e.kind == "token_transfer")


def test_same_code_other_network_only_config_changes(op_cfg):
    b = replay_bundle(op_cfg, OP_TX, "op_usdc_transfer")
    assert b.network == "optimism-mainnet"
    assert any("199.820981 USDC" in e.text for e in b.items)
    assert b.items[0].sources[0].url.startswith("https://explorer.optimism.io/tx/")


def test_explorer_down_falls_back_to_rpc(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={"eth.blockscout.com"})
    assert b.status == "success"
    assert b.items[0].text.startswith("(From RPC only)")
    assert any(g.what == "Call decoding" for g in b.gaps)


def test_rpc_down_keeps_explorer_facts_and_declares_gap(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={"ethereum-rpc.publicnode.com"})
    assert b.status == "success"
    assert not any(e.kind == "cross_check" for e in b.items)
    assert any(g.what == "RPC transaction data" for g in b.gaps)


def test_everything_down_is_unknown_not_a_crash(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={"eth.blockscout.com", "ethereum-rpc.publicnode.com"})
    assert b.status == "unknown" and b.items == [] and len(b.gaps) == 2


def test_rejects_invalid_hash(eth_cfg):
    with pytest.raises(ValueError, match="valid transaction hash"):
        build_bundle("0x1234", eth_cfg)
