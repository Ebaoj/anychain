"""The evidence bundle on real transactions: happy paths and every kind of trouble."""
import pytest

from anychain.bundle import amount, build_bundle, describe_revert
from tests.conftest import (
    CREATION_TX, ERC721_TX, ERC1155_TX, EXPLORER_HOST, FAILED_TX, OP_TX, PENDING_TX, RPC_HOST,
    SWAP_TX, USDC_TX, make_transport, mutated, replay_bundle,
)

USDC_PROXY = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_IMPL = "0x43506849D7C04F9138D1A2050bbF3A0c054402dd"
RECEIPT = f'eth_getTransactionReceipt ["{USDC_TX}"]'
RPC_TX = f'eth_getTransactionByHash ["{USDC_TX}"]'


def _rpc_result(fixture, suffix, change):
    """Override for one RPC call: the real recorded result, edited by `change(result)`."""
    def edit(body):
        body["result"] = change(body["result"])
    return mutated(fixture, suffix, edit)


def _texts(bundle):
    return " ".join(e.text for e in bundle.items)


def _gap(bundle, what):
    return next((g for g in bundle.gaps if g.what == what), None)


# ---- helpers ---------------------------------------------------------------

def test_amount_is_exact():
    assert amount(69348400, 6) == "69.3484"
    assert amount(10**18, 18) == "1"
    assert amount(1, 18) == "0.000000000000000001"
    assert amount(5, 0) == "5"


def test_revert_reason_formats():
    decoded = {"method_call": "Error(string reason)", "parameters": [{"name": "reason", "value": "EXPIRED"}]}
    assert describe_revert(decoded) == "Error(string reason) with reason='EXPIRED'"
    assert describe_revert({"raw": "0x1234"}) == "undecoded revert data 0x1234"


# ---- successful transactions -------------------------------------------------

def test_usdc_transfer_facts(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    assert b.status == "success"
    assert "69.3484 USDC" in _texts(b)
    assert "transfer(address,uint256)" in _texts(b)
    assert "proxy -> FiatTokenV2_2" in _texts(b)
    assert b.gaps == []


def test_every_fact_has_a_source_and_ids_are_sequential(eth_cfg):
    b = replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap")
    for ev in b.items:
        assert ev.sources and all(s.url or s.detail for s in ev.sources)
    assert [e.id for e in b.items] == [f"E{i}" for i in range(1, len(b.items) + 1)]


def test_swap_has_transfers_events_and_cross_check(eth_cfg):
    b = replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap")
    kinds = {e.kind for e in b.items}
    assert {"overview", "fee", "call", "token_transfer", "internal_call", "event", "cross_check"} <= kinds
    assert "Swap" in [e.data.get("event") for e in b.items if e.kind == "event"]
    assert any("BUNKER" in e.text for e in b.items if e.kind == "token_transfer")


def test_transfer_logs_are_not_repeated_as_events(eth_cfg):
    b = replay_bundle(eth_cfg, SWAP_TX, "eth_uniswap_v2_swap")
    assert "Transfer" not in [e.data.get("event") for e in b.items if e.kind == "event"]


def test_same_code_other_network_only_config_changes(op_cfg):
    b = replay_bundle(op_cfg, OP_TX, "op_usdc_transfer")
    assert b.network == "optimism-mainnet"
    assert "199.820981 USDC" in _texts(b)
    assert b.items[0].sources[0].url.startswith("https://explorer.optimism.io/tx/")


def test_nft_transfers_show_token_ids(eth_cfg):
    nft = replay_bundle(eth_cfg, ERC721_TX, "eth_erc721_transfer")
    assert "token #443098" in _texts(nft)
    multi = replay_bundle(eth_cfg, ERC1155_TX, "eth_erc1155_transfer")
    assert "1 x " in _texts(multi) and "token #16" in _texts(multi)


# ---- failed, pending, creation ------------------------------------------------

def test_failed_tx_reports_reason_fee_and_no_value_moved(eth_cfg):
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot")
    assert b.status == "failed"
    assert "reason='INSUFFICIENT_OUTPUT_AMOUNT'" in _texts(b)
    assert "charged even though the transaction failed" in _texts(b)
    assert "not transferred because the transaction reverted" in _texts(b)
    assert "Native value sent" not in _texts(b)


def test_unverified_contract_is_declared_not_guessed(eth_cfg):
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot")
    assert "not decoded" in _texts(b)
    gap = _gap(b, "Call decoding")
    assert gap and not gap.retryable
    assert "none" in b.abi_sources.values()


def test_pending_tx_has_no_fee_and_a_retryable_gap(eth_cfg):
    b = replay_bundle(eth_cfg, PENDING_TX, "eth_pending_swap")
    assert b.status == "pending"
    assert [e.kind for e in b.items] == ["overview"]
    assert _gap(b, "Final outcome").retryable
    assert _gap(b, "RPC transaction data") is None  # the node not knowing a pending tx is expected


def test_pending_tx_still_reports_rpc_outage(eth_cfg):
    b = replay_bundle(eth_cfg, PENDING_TX, "eth_pending_swap", offline_hosts={RPC_HOST})
    assert _gap(b, "RPC transaction data").retryable


def test_contract_creation_names_the_new_contract(eth_cfg):
    b = replay_bundle(eth_cfg, CREATION_TX, "eth_contract_creation")
    assert "deployed 0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f (UniswapV2Factory)" in _texts(b)


def test_old_tx_missing_on_node_blames_history_not_the_hash(eth_cfg):
    # Real behaviour: publicnode returns null for this 2020 transaction.
    b = replay_bundle(eth_cfg, CREATION_TX, "eth_contract_creation")
    assert "archive" in _gap(b, "RPC transaction data").needed


def test_slow_explorer_endpoint_becomes_a_retryable_gap(eth_cfg):
    # Real behaviour, recorded: /internal-transactions timed out for this 2020 tx
    # (22.7s seen with curl, over the 15s timeout). The fixture stores the timeout itself.
    transport = make_transport("eth_contract_creation")
    b = replay_bundle(eth_cfg, CREATION_TX, "eth_contract_creation", transport=transport)
    gap = _gap(b, "Internal calls")
    assert gap.retryable and "ReadTimeout" in gap.why
    assert sum(k.endswith("/internal-transactions") for k in transport.calls) == 3  # retried
    assert "deployed 0x5C69" in _texts(b)  # the rest of the answer survived


# ---- degradation -----------------------------------------------------------------

def test_explorer_down_falls_back_to_rpc(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={EXPLORER_HOST})
    assert b.status == "success"
    assert b.items[0].text.startswith("(From RPC only)")
    assert _gap(b, "Call decoding").retryable


def test_rpc_down_keeps_explorer_facts_and_declares_gap(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={RPC_HOST})
    assert b.status == "success"
    assert not any(e.kind == "cross_check" for e in b.items)
    assert _gap(b, "RPC transaction data").retryable


def test_everything_down_is_unknown_not_a_crash(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={EXPLORER_HOST, RPC_HOST})
    assert b.status == "unknown" and b.items == [] and len(b.gaps) == 2
    assert all(g.retryable for g in b.gaps)


def test_rpc_on_wrong_chain_is_ignored(eth_cfg, op_cfg):
    eth_cfg.network.chain_id = 10  # config says Optimism, RPC answers chain 1
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    gap = _gap(b, "RPC transaction data")
    assert "chain 1" in gap.why and not gap.retryable
    assert not any(e.kind == "cross_check" for e in b.items)


def test_abi_lookup_failure_is_not_called_unverified(eth_cfg):
    busy = {"status": 503, "body": "busy"}
    overrides = {f"/smart-contracts/{USDC_PROXY}": busy, f"/smart-contracts/{USDC_IMPL}": busy}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    assert _gap(b, "ABI lookup").retryable
    call_gap = _gap(b, "Call decoding")
    assert call_gap.retryable and "lookup failed" in call_gap.why
    assert "69.3484 USDC" in _texts(b)  # transfers do not need the ABI


def test_explorer_abi_can_be_disabled_by_config(eth_cfg):
    eth_cfg.abi_strategy.order = ["signature_db"]
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    assert "disabled in config" in next(iter(b.abi_sources.values()))
    assert _gap(b, "Call decoding")


def test_rejects_invalid_hash(eth_cfg):
    with pytest.raises(ValueError, match="valid transaction hash"):
        build_bundle("0x1234", eth_cfg)


# ---- status edge cases (real recordings with one field changed) -------------------

def _dropped(body):
    # Blockscout stores dropped txs as status "error" + result "dropped/replaced".
    body.update(status="error", result="dropped/replaced", block_number=None, revert_reason=None)


def test_dropped_tx_is_not_called_failed(eth_cfg):
    unknown_to_node = {f'eth_getTransactionByHash ["{FAILED_TX}"]': {
        "status": 200, "body": '{"jsonrpc":"2.0","id":1,"result":null}'}}
    overrides = mutated("eth_failed_unverified_bot", FAILED_TX, _dropped) | unknown_to_node
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot", overrides=overrides)
    assert b.status == "dropped"
    assert "never executed" in _texts(b)
    assert not any(e.kind in ("fee", "revert") for e in b.items)
    assert _gap(b, "RPC transaction data") is None  # a dropped tx is expected to be unknown to the node


def test_dropped_on_explorer_but_mined_on_node_is_flagged(eth_cfg):
    overrides = mutated("eth_failed_unverified_bot", FAILED_TX, _dropped)
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot", overrides=overrides)
    assert b.status == "failed"  # the node's receipt wins
    assert "shows this transaction as dropped" in _gap(b, "Explorer index").why


def test_failure_reason_from_result_when_no_revert_reason(eth_cfg):
    def out_of_gas(body):
        body.update(revert_reason=None, result="out of gas")
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot", overrides=mutated(
        "eth_failed_unverified_bot", FAILED_TX, out_of_gas))
    assert "failure as: 'out of gas'" in _texts(b)
    assert _gap(b, "Revert reason") is None


def test_pre_byzantium_receipt_is_not_read_as_failed(eth_cfg):
    def no_status(receipt):
        receipt.pop("status")
        return receipt
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=_rpc_result("eth_usdc_transfer", RECEIPT, no_status))
    assert b.status == "success"
    assert not any(e.kind == "cross_check" for e in b.items)
    assert _gap(b, "Status disagreement") is None


def test_rpc_only_contract_creation(eth_cfg):
    created = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
    def creation_tx(tx):
        return tx | {"to": None}
    def creation_receipt(receipt):
        return receipt | {"contractAddress": created}
    overrides = _rpc_result("eth_usdc_transfer", RPC_TX, creation_tx) | _rpc_result("eth_usdc_transfer", RECEIPT, creation_receipt)
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={EXPLORER_HOST}, overrides=overrides)
    assert f"contract creation of {created}" in _texts(b)
    assert _gap(b, "Call decoding") is None  # init code is not a function call


def test_rpc_only_pending(eth_cfg):
    overrides = _rpc_result("eth_usdc_transfer", RECEIPT, lambda _r: None)
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={EXPLORER_HOST}, overrides=overrides)
    assert b.status == "pending" and _gap(b, "Final outcome").retryable


def test_explorer_lagging_behind_rpc_uses_the_receipt(eth_cfg):
    def still_pending(body):
        body.update(status=None, result="pending", block_number=None)
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=mutated("eth_usdc_transfer", USDC_TX, still_pending))
    assert b.status == "success"
    assert b.items[0].text.startswith("(From RPC only)")
    assert _gap(b, "Explorer index").retryable
    assert "transfer(address,uint256)" in _texts(b)  # explorer is up, so its ABI is still used


def test_bad_chain_id_answer_is_a_gap_not_a_crash(eth_cfg):
    overrides = {"eth_chainId []": {"status": 200, "body": '{"jsonrpc":"2.0","id":1,"result":null}'}}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    assert "69.3484 USDC" in _texts(b)
    assert not _gap(b, "RPC transaction data").retryable


def test_time_budget_caps_the_whole_explanation(eth_cfg):
    import httpx
    from anychain.bundle import BundleBuilder
    from anychain.collectors.explorer import ExplorerClient
    from anychain.collectors.http import Budget
    from anychain.collectors.rpc import RpcClient
    transport = make_transport("eth_usdc_transfer")
    client, budget = httpx.Client(transport=transport), Budget(0)
    b = BundleBuilder(eth_cfg, ExplorerClient(eth_cfg.explorer, client, budget), RpcClient(eth_cfg.rpc, client, budget)).build(USDC_TX)
    assert transport.calls == []  # nothing was even attempted
    assert b.status == "unknown" and all(g.retryable and "time budget" in g.why for g in b.gaps)


def test_explorer_404_with_mined_receipt_is_index_lag(eth_cfg):
    overrides = {USDC_TX: {"status": 404, "body": "{}"}}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    gap = _gap(b, "Explorer transaction data")
    assert gap.retryable and "not indexed" in gap.why
    assert b.status == "success"


def test_empty_explorer_body_is_not_invented_as_pending(eth_cfg):
    overrides = {USDC_TX: {"status": 200, "body": "{}"}}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    assert b.status != "pending" and _gap(b, "Explorer index") is None


def test_abi_lookup_bad_request_is_consistently_not_retryable(eth_cfg):
    bad = {"status": 400, "body": "bad"}
    overrides = {f"/smart-contracts/{USDC_PROXY}": bad, f"/smart-contracts/{USDC_IMPL}": bad}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    assert not _gap(b, "ABI lookup").retryable and not _gap(b, "Call decoding").retryable


def test_malformed_receipt_is_a_gap(eth_cfg):
    overrides = {RECEIPT: {"status": 200, "body": '{"jsonrpc":"2.0","id":1,"result":[]}'}}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
    assert "expected an object" in _gap(b, "RPC transaction data").why
