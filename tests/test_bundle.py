"""The evidence bundle on real transactions: happy paths and every kind of trouble."""
import pytest

from anychain.bundle import amount, build_bundle
from tests.conftest import (
    CREATION_TX, DATA_TO_EOA_TX, ERC721_TX, ERC1155_TX, ETH_FIXTURES, EXECUTE_7702_TX, EXPLORER_HOST, FAILED_TX,
    INVALID_NONCE_7702_TX, LIDO_TX, MAKER_TX, OP_TX, SET_THEN_REVOKED_7702_TX, PENDING_TX, REVOKE_7702_TX, RPC_HOST, SAFE_DEPLOY_TX, SWAP_TX, USDC_TX,
    make_transport, mutated, recorded_body, replay_bundle,
)

USDC_PROXY = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_IMPL = "0x43506849D7C04F9138D1A2050bbF3A0c054402dd"
RECEIPT = f'eth_getTransactionReceipt ["{USDC_TX}"]'
RPC_TX = f'eth_getTransactionByHash ["{USDC_TX}"]'


def _records(fixture):
    import json
    from tests.conftest import FIXTURES
    return json.loads((FIXTURES / f"{fixture}.json").read_text())


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
    events = [e.data.get("event") for e in b.items if e.kind == "event"]
    assert events == ["Sync", "Swap"]  # decoding works, and only the non-transfer logs are events
    recorded_logs = recorded_body("eth_uniswap_v2_swap", "/logs")["items"]
    recorded_transfers = recorded_body("eth_uniswap_v2_swap", "/token-transfers")["items"]
    token_facts = [e for e in b.items if e.kind == "token_transfer"]
    assert len(token_facts) == len(recorded_transfers)
    assert len(token_facts) + len(events) == len(recorded_logs)


def test_same_code_other_network_only_config_changes(op_cfg):
    b = replay_bundle(op_cfg, OP_TX, "op_usdc_transfer")
    assert b.network == "optimism-mainnet"
    assert "199.820981 USDC" in _texts(b)
    assert b.items[0].sources[0].url.startswith("https://explorer.optimism.io/tx/")


def test_nft_transfers_show_token_ids(eth_cfg):
    nft = replay_bundle(eth_cfg, ERC721_TX, "eth_erc721_transfer")
    assert "token #443098" in _texts(nft)
    multi = replay_bundle(eth_cfg, ERC1155_TX, "eth_erc1155_transfer")
    assert "Token transfer: 1 x 0xDF402d540A0aE8B866181590fcfcbBC56d2b25e8 token #16 " \
           "from 0x0000000000000000000000000000000000000000 to 0xFDBbE7ACAE6cAa050D1c0B29dE51b6e11Ce81bEF." \
           in _texts(multi)  # this token has no symbol or name on the explorer, so its address is shown


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
    # Recorded with the explorer unreachable, so the node's real eth_getCode answers are in the
    # fixture: the old block is refused (no archive state), today's code shows USDC is a contract.
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER_HOST})
    assert b.status == "success"
    assert b.items[0].text.startswith("(From RPC only)")
    assert "Called function with selector 0xa9059cbb" in _texts(b)
    assert _gap(b, "Call decoding").retryable
    assert _gap(b, "Code at execution time") is None


def test_rpc_only_receipt_without_status_says_outcome_unknown(eth_cfg):
    def no_status(receipt):
        receipt.pop("status")
        return receipt
    overrides = _rpc_result("eth_usdc_rpc_only", RECEIPT, no_status)
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER_HOST}, overrides=overrides)
    assert b.status == "unknown" and "outcome not recorded" in b.items[0].text
    assert "predates it" in _gap(b, "Outcome").why


def test_rpc_only_type4_is_not_described_as_settled(eth_cfg):
    def with_authorization(tx):
        return tx | {"type": "0x4", "authorizationList": [{"address": "0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B"}]}
    overrides = _rpc_result("eth_usdc_rpc_only", RPC_TX, with_authorization)
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER_HOST}, overrides=overrides)
    delegation = next(e for e in b.items if e.kind == "delegation")
    assert "not checked without the explorer" in delegation.text and delegation.data["applied"] is None


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
    usdc_code_today = {k: v for k, v in _records("eth_usdc_rpc_only").items() if '"latest"]' in k}  # real node answer
    overrides = mutated("eth_usdc_transfer", USDC_TX, still_pending) | {k.split(" ", 2)[2]: v for k, v in usdc_code_today.items()}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", overrides=overrides)
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
    assert "expected a transaction or receipt" in _gap(b, "RPC transaction data").why


# ---- transaction types found by the Fable bug hunt (all real recordings) ----------

def test_delegatecall_value_is_not_a_transfer(eth_cfg):
    b = replay_bundle(eth_cfg, LIDO_TX, "eth_lido_submit")
    internal = [e for e in b.items if e.kind == "internal_call"]
    assert not any("ETH transfer" in e.text for e in internal)
    lido = next(e for e in internal if "(Lido)" in e.text)
    assert lido.text.startswith("Internal delegatecall") and "no ETH moved" in lido.text
    assert lido.data["moves_value"] is False
    assert "0.046651189999999999 STETH" in _texts(b)  # what the user received


def test_failed_internal_call_with_value_moves_nothing(eth_cfg):
    def fail_with_value(body):
        body["items"][0].update(type="call", value="1000", success=False)
    overrides = mutated("eth_lido_submit", "/internal-transactions", fail_with_value)
    b = replay_bundle(eth_cfg, LIDO_TX, "eth_lido_submit", overrides=overrides)
    first = next(e for e in b.items if e.kind == "internal_call")
    assert "but failed; nothing was transferred" in first.text and first.data["moves_value"] is False


def test_7702_delegation_cleared_is_not_a_plain_transfer(eth_cfg):
    b = replay_bundle(eth_cfg, REVOKE_7702_TX, "eth_7702_revoke")
    assert "Code delegation cleared by this transaction: 0x4269D7f3e812FF9cDB73577b089458b3f42BFdC4" in _texts(b)
    assert "carries 1 EIP-7702 authorization(s)" in _texts(b)
    assert "Plain ETH transfer" not in _texts(b)
    assert b.items[0].data["tx_type"] == 4


def test_7702_execute_tx_states_delegation_and_decodes(eth_cfg):
    b = replay_bundle(eth_cfg, EXECUTE_7702_TX, "eth_7702_execute")
    assert "runs the code of 0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B" in _texts(b)
    assert "Called execute(bytes32,bytes)" in _texts(b)


def test_data_to_account_without_code_today_is_not_called_a_non_call(eth_cfg):
    transport = make_transport("eth_data_to_eoa")
    b = replay_bundle(eth_cfg, DATA_TO_EOA_TX, "eth_data_to_eoa", transport=transport)
    assert "does not say whether this was a function call" in _texts(b) and "selector" not in _texts(b)
    assert _gap(b, "Call decoding") is None
    assert "execution trace" in _gap(b, "Code at execution time").needed
    to = "0x9BE0c82d5bA973a9e6861695626D4F9983e80C88"
    assert not any(k.endswith(f"/smart-contracts/{to}") for k in transport.calls)  # no pointless ABI lookup
    assert not any("eth_getCode" in k for k in transport.calls)  # old state is not used as proof


def test_precompile_is_never_called_a_codeless_account(op_cfg):
    # Real OP tx to the P256 precompile at 0x...0100: it executes with no stored code.
    tx = "0x0032185da44673d9610ea7c0fee89b04845c21cb29c7e7c247797496961be40a"
    b = replay_bundle(op_cfg, tx, "op_p256_precompile")
    assert "not a function call" not in _texts(b)
    assert "does not say whether this was a function call" in _texts(b)


def test_delegation_cleared_in_the_same_tx_is_asserted(eth_cfg):
    to = "0x9BE0c82d5bA973a9e6861695626D4F9983e80C88"
    def cleared_here(body):
        body["authorization_list"] = [{"authority": to, "address_hash": "0x" + "0" * 40, "status": "ok"}]
    overrides = mutated("eth_data_to_eoa", DATA_TO_EOA_TX, cleared_here)
    b = replay_bundle(eth_cfg, DATA_TO_EOA_TX, "eth_data_to_eoa", overrides=overrides)
    assert "whose code delegation this transaction cleared before running" in _texts(b)
    assert _gap(b, "Code at execution time") is None


def test_superseded_authorization_is_not_stated_as_in_effect(eth_cfg):
    authority = "0x5718713439b078594dc4c96cbBB0E4F9491dF050"
    def set_then_replace(body):
        first = next(a for a in body["authorization_list"] if a["authority"] == authority)
        body["authorization_list"].append(first | {"address_hash": "0x" + "0" * 40})  # later: cleared
    overrides = mutated("eth_7702_invalid_nonce", INVALID_NONCE_7702_TX, set_then_replace)
    b = replay_bundle(eth_cfg, INVALID_NONCE_7702_TX, "eth_7702_invalid_nonce", overrides=overrides)
    facts = [e.text for e in b.items if e.kind == "delegation" and authority in e.text]
    assert any("replaced by a later authorization" in f for f in facts)
    assert any("Code delegation cleared by this transaction" in f for f in facts)
    assert not any("from here on" in f for f in facts)


def test_7702_delegation_set_in_same_tx_counts_as_code(eth_cfg):
    # The account delegated in this tx and revoked later: today the explorer says "no code".
    b = replay_bundle(eth_cfg, SET_THEN_REVOKED_7702_TX, "eth_7702_set_then_revoked")
    assert "Called execute(bytes32,bytes) on 0x70fB4195638281fAA69472DBD7D8f9B65f535E7F" in _texts(b)
    assert "no contract code" not in _texts(b)


def test_7702_invalid_authorization_is_not_stated_as_applied(eth_cfg):
    b = replay_bundle(eth_cfg, INVALID_NONCE_7702_TX, "eth_7702_invalid_nonce")
    invalid = next(e for e in b.items if e.kind == "delegation" and "0x862B237a" in e.text)
    assert "was not applied: the explorer marks it 'invalid_nonce'" in invalid.text
    assert "from here on" not in invalid.text and invalid.data["applied"] is False


def test_internal_call_without_success_flag_is_not_called_a_transfer(eth_cfg):
    def unknown_success(body):
        body["items"][0].update(type="call", value="1000")
        body["items"][0].pop("success", None)
    overrides = mutated("eth_lido_submit", "/internal-transactions", unknown_success)
    b = replay_bundle(eth_cfg, LIDO_TX, "eth_lido_submit", overrides=overrides)
    first = next(e for e in b.items if e.kind == "internal_call")
    assert "does not say if it succeeded" in first.text and first.data["moves_value"] is None


def test_internal_create_names_the_deployed_contract(eth_cfg):
    b = replay_bundle(eth_cfg, SAFE_DEPLOY_TX, "eth_safe_deploy")
    assert "Internal create by 0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67 (SafeProxyFactory): " \
           "deployed 0x9542f2eC81233818a18346cA2D1E673aA7db7E12." in _texts(b)
    assert "(none)" not in _texts(b)


def test_anonymous_events_decode_when_exactly_one_fits(eth_cfg):
    b = replay_bundle(eth_cfg, MAKER_TX, "eth_maker_vat")
    vat = [e for e in b.items if e.kind == "event" and "(Vat)" in e.text]
    assert vat and all("LogNote(" in e.text and "(anonymous event)" in e.text for e in vat)
    assert "topic0 0xbb35783b" not in _texts(b)
    assert not any("0x35D1b3F3D7966A1DFe207aa4514C12a259A0492B" in g.why for g in b.gaps)


@pytest.mark.parametrize("fixture,tx_hash", sorted(ETH_FIXTURES.items()))
def test_no_internal_jargon_reaches_the_user(eth_cfg, fixture, tx_hash):
    b = replay_bundle(eth_cfg, tx_hash, fixture)
    words = _texts(b) + " ".join(f"{g.why} {g.needed}" for g in b.gaps)
    for jargon in ("Byzantium", "phase 2", "phase"):
        assert jargon not in words


# ---- seventh review (Fable) ------------------------------------------------------

def test_normal_event_on_contract_with_anonymous_events_keeps_topic0(eth_cfg):
    # Real DeFi Saver recipe: DSProxy's ABI has an anonymous LogNote, but these logs are
    # normal ActionEvent logs from delegatecalled actions, missing from the DSProxy ABI.
    tx = "0x92f208d329d76c8e557a0f64c7527efca13ef7748f9cc56ebb498f90f25e172a"
    b = replay_bundle(eth_cfg, tx, "eth_dsproxy_recipe")
    assert "Event with topic0 0x2b6d22f4" in _texts(b)
    assert "anonymous events in its ABI" not in _texts(b)
    assert not any("declares anonymous events" in g.why for g in b.gaps)


def test_authorization_without_delegate_field_is_unknown_not_cleared(eth_cfg):
    def rename(body):
        for auth in body["authorization_list"]:
            auth["delegate_renamed"] = auth.pop("address_hash")
    overrides = mutated("eth_7702_execute", EXECUTE_7702_TX, rename)
    b = replay_bundle(eth_cfg, EXECUTE_7702_TX, "eth_7702_execute", overrides=overrides)
    assert "does not report the new target" in _texts(b)
    assert "not a function call" not in _texts(b) and "runs the code of None" not in _texts(b)


def test_authorization_with_legacy_address_field_is_read(eth_cfg):
    def legacy(body):
        for auth in body["authorization_list"]:
            auth["address"] = auth.pop("address_hash")
    overrides = mutated("eth_7702_execute", EXECUTE_7702_TX, legacy)
    b = replay_bundle(eth_cfg, EXECUTE_7702_TX, "eth_7702_execute", overrides=overrides)
    assert "runs the code of 0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B" in _texts(b)


def test_rpc_only_failed_code_read_keeps_selector_and_is_retryable(eth_cfg):
    overrides = {'eth_getCode ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "latest"]': {"timeout": True}}
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER_HOST}, overrides=overrides)
    assert "Called function with selector 0xa9059cbb" in _texts(b)
    assert _gap(b, "Contract check").retryable
    assert _gap(b, "Code at execution time") is None


def test_undecoded_call_on_delegated_account_names_the_delegate(op_cfg):
    tx = "0x86aec918026c7099a71e63d616ccba848a5bb1a03e39c6f5314a615c04733923"
    b = replay_bundle(op_cfg, tx, "op_7702_batch")
    gap = _gap(b, "Call decoding")
    assert "ran the code of 0xec15C4d4" in gap.why and gap.needed.startswith("0xec15C4d4")


def test_superseded_authorization_is_not_marked_applied(eth_cfg):
    authority = "0x5718713439b078594dc4c96cbBB0E4F9491dF050"
    def set_then_replace(body):
        first = next(a for a in body["authorization_list"] if a["authority"] == authority)
        body["authorization_list"].append(first | {"address_hash": "0x" + "0" * 40})
    overrides = mutated("eth_7702_invalid_nonce", INVALID_NONCE_7702_TX, set_then_replace)
    b = replay_bundle(eth_cfg, INVALID_NONCE_7702_TX, "eth_7702_invalid_nonce", overrides=overrides)
    replaced = next(e for e in b.items if e.kind == "delegation" and "replaced by a later" in e.text)
    assert replaced.data["applied"] is False and replaced.data["superseded"] is True


def test_internal_create_without_success_flag_is_not_asserted(eth_cfg):
    def unknown(body):
        create = next(i for i in body["items"] if i["type"] == "create")
        create.pop("success", None)
    overrides = mutated("eth_safe_deploy", "/internal-transactions", unknown)
    b = replay_bundle(eth_cfg, SAFE_DEPLOY_TX, "eth_safe_deploy", overrides=overrides)
    create = next(e for e in b.items if e.kind == "internal_call" and "create" in e.text)
    assert "does not say if the deployment succeeded" in create.text and create.data["moves_value"] is None


# ---- every problem gap names the right cause (the event log alerts on these) ----------

def _causes(b, what):
    return {g.cause for g in b.gaps if g.what == what}


def test_our_own_failure_is_a_processing_error(monkeypatch, eth_cfg):
    from anychain.bundle import BundleBuilder

    def boom(*_a, **_k):
        raise KeyError("odd")
    monkeypatch.setattr(BundleBuilder, "_add_fee", boom)
    monkeypatch.setattr(BundleBuilder, "_add_one_event", boom)
    b = replay_bundle(eth_cfg, MAKER_TX, "eth_maker_vat")  # has events no transfer covers
    assert _causes(b, "Fee") == {"processing_error"}
    assert _causes(b, "Events") == {"processing_error"}


def test_a_refusing_source_is_a_source_error(eth_cfg):
    b = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer",
                      overrides={"/token-transfers": {"status": 400, "body": "{}"}})
    assert _causes(b, "Token transfers") == {"source_error"}


def test_an_rpc_on_the_wrong_chain_is_a_config_error(eth_cfg):
    cfg = eth_cfg.model_copy(deep=True)
    cfg.network.chain_id = 10
    b = replay_bundle(cfg, USDC_TX, "eth_usdc_transfer")
    assert _causes(b, "RPC transaction data") == {"config_error"}


def test_an_unreadable_log_from_the_explorer_is_a_source_error(eth_cfg):
    def drop_emitter(body):
        for item in body["items"]:
            item["address"] = None
    b = replay_bundle(eth_cfg, MAKER_TX, "eth_maker_vat",
                      overrides=mutated("eth_maker_vat", "/logs", drop_emitter))
    assert "source_error" in _causes(b, "Events") and "processing_error" not in _causes(b, "Events")


# ---- internal calls: value-moving calls survive the cutoff (C11); parent-reverted wording (C12) ----

CUTOFF_TX = "0xd58d09060a633d461c0d260bfdb4f61bd4ab84a9ba5039b3f5f9ac5effe935a3"  # 122 internal calls


def _all_internal_items(fixture: str) -> list[dict]:
    """Every page of the recorded internal-transactions list (the explorer pages by 50)."""
    import json
    from tests.conftest import FIXTURES
    records = json.loads((FIXTURES / f"{fixture}.json").read_text())
    pages = [json.loads(v["body"])["items"] for k, v in records.items() if "/internal-transactions" in k]
    return [it for page in pages for it in page]


def test_calls_that_moved_value_are_listed_even_beyond_the_cutoff(eth_cfg):
    b = replay_bundle(eth_cfg, CUTOFF_TX, "eth_internal_value_beyond_cutoff")
    items = _all_internal_items("eth_internal_value_beyond_cutoff")
    moved = [it for it in items if it["type"] != "staticcall" and int(it["value"]) > 0]
    assert len(moved) == 3  # the explorer's own list: indices 48, 70, 95
    stated = [e for e in b.items if e.kind == "internal_call" and e.data.get("value") not in (None, "0")
              and e.data.get("type")]
    assert sorted(int(e.data["value"]) for e in stated) == sorted(int(it["value"]) for it in moved)
    [gap] = [g for g in b.gaps if g.what == "Internal calls"]
    assert "showing 30 of 93 internal calls; every internal call that carried ETH is listed" in gap.why


def test_a_call_undone_by_its_parent_is_not_called_failed(eth_cfg):
    b = replay_bundle(eth_cfg, FAILED_TX, "eth_failed_unverified_bot")
    items = _all_internal_items("eth_failed_unverified_bot")
    errors = {it.get("error") for it in items}
    assert {"Reverted", "Parent reverted"} <= errors  # both kinds are in this real recording
    texts = [e.text for e in b.items if e.kind == "internal_call"]
    assert any("(this internal call reverted)" in t for t in texts)
    assert any("(undone because a call above it reverted)" in t for t in texts)
    assert not any("(this internal call failed)" in t for t in texts)


def test_failure_note_quotes_any_other_explorer_reason():
    from anychain.bundle import _failure_note
    from anychain.collectors.types import InternalCall
    call = lambda error: InternalCall("call", None, None, None, 0, False, error)
    assert _failure_note(call("out of gas"), short=True) == "this internal call failed: the explorer reports 'out of gas'"
    assert _failure_note(call(None), short=True) == "this internal call failed"


def test_cutoff_keeps_the_explorers_order(eth_cfg):
    b = replay_bundle(eth_cfg, CUTOFF_TX, "eth_internal_value_beyond_cutoff")
    items = sorted((it for it in _all_internal_items("eth_internal_value_beyond_cutoff") if it["type"] != "staticcall"),
                   key=lambda it: it["index"])  # the explorer's order: pages of increasing trace index
    stated = [e for e in b.items if e.kind == "internal_call" and e.data.get("type")]
    values = [int(e.data["value"]) for e in stated if e.data["value"] != "0"]
    assert values == [int(it["value"]) for it in items if int(it["value"]) > 0]


def test_cutoff_claim_is_scoped_when_system_calls_carried_value(monkeypatch):
    # zkSync: calls between system contracts are summed, not listed; if they moved ETH,
    # the cutoff claim must not cover them.
    from anychain.collectors.types import InternalCall
    from tests.test_networks import _cfg
    from tests.test_golden import _case
    cfg = _cfg("zksync-era")
    _config, tx = _case("zksync_paymaster")
    real = __import__("anychain.collectors.explorer", fromlist=["ExplorerClient"]).ExplorerClient.internal_transactions

    def padded(self, tx_hash):
        items, truncated = real(self, tx_hash)
        system = next(it for it in items if it.sender and it.recipient and it.sender.address and it.recipient.address
                      and int(it.sender.address, 16) <= 0xffff and int(it.recipient.address, 16) <= 0xffff)
        ordinary = next(it for it in items if it.type == "call" and it is not system
                        and not (int(it.sender.address, 16) <= 0xffff and int(it.recipient.address, 16) <= 0xffff))
        filler = [ordinary] * 40  # push past the 30-call cutoff
        return [InternalCall(system.type, system.sender, system.recipient, None, 5, True)] + filler, truncated
    monkeypatch.setattr("anychain.collectors.explorer.ExplorerClient.internal_transactions", padded)
    b = replay_bundle(cfg, tx, "zksync_paymaster")
    [gap] = [g for g in b.gaps if g.what == "Internal calls"]
    assert "every internal call outside the system-contract group that carried ETH is listed" in gap.why
