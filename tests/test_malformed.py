"""Malformed payloads: start from real recorded responses, then break one field.

Real explorers rarely send these, but when they do, the user must get a partial
answer with a declared gap, never a stack trace.
"""
from tests.conftest import SWAP_TX, USDC_TX, mutated, replay_bundle


def _set(path, value):
    def change(body):
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


def _ok(bundle):
    assert bundle.items, "some facts must survive"
    return bundle


def _texts(bundle):
    return " ".join(e.text for e in bundle.items)


def test_null_topics_on_a_log(eth_cfg):
    # items 0-2 are token transfers (skipped as events); item 3 is the pair's Sync event.
    overrides = mutated("eth_uniswap_v2_swap", "/logs", _set(["items", 3, "topics"], None))
    b = _ok(replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap", overrides=overrides))
    events = [e for e in b.items if e.kind == "event"]
    assert any("topic0 (none)" in e.text for e in events)  # the broken log is reported as undecoded
    assert "Swap" in [e.data.get("event") for e in events]  # the other logs still decode
    assert "Sync" not in [e.data.get("event") for e in events]


def test_null_items_list(eth_cfg):
    overrides = mutated("eth_uniswap_v2_swap", "/logs", _set(["items"], None))
    b = _ok(replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap", overrides=overrides))
    assert not any(e.kind == "event" for e in b.items)


def test_null_value_on_the_transaction(eth_cfg):
    overrides = mutated("eth_usdc_transfer", USDC_TX, _set(["value"], None))
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert "Native value sent: unknown" in b.items[0].text
    assert "69.3484 USDC" in _texts(b) and b.gaps == []  # nothing else was affected


def test_response_is_a_list_instead_of_an_object(eth_cfg):
    overrides = {"/token-transfers": {"status": 200, "body": "[]"}}
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert any(g.what == "Token transfers" for g in b.gaps)


def test_html_instead_of_json(eth_cfg):
    overrides = {"/internal-transactions": {"status": 200, "body": "<html>Just a moment...</html>"}}
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert next(g for g in b.gaps if g.what == "Internal calls").retryable


def test_token_without_decimals_shows_raw_amount(eth_cfg):
    def drop_decimals(body):
        body["items"][0]["token"]["decimals"] = None
        body["items"][0]["total"]["decimals"] = None
    overrides = mutated("eth_usdc_transfer", "/token-transfers", drop_decimals)
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert "69348400 raw units of USDC (decimals unknown)" in " ".join(e.text for e in b.items)


def test_garbage_calldata(eth_cfg):
    overrides = mutated("eth_usdc_transfer", USDC_TX, _set(["raw_input"], "0xnothex"))
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert any(g.what == "Call decoding" for g in b.gaps)
    assert "69.3484 USDC" in _texts(b)


def test_odd_proxy_metadata(eth_cfg):
    def odd(body):
        body["abi"] = "not a list"
        body["implementations"] = ["0xnot-a-dict"]
    overrides = mutated("eth_usdc_transfer", "/smart-contracts/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", odd)
    b = _ok(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides))
    assert "transfer(address,uint256)" in _texts(b)  # still decoded via the implementation from the tx payload
