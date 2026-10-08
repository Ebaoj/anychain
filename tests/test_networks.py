"""Portability: the same checks on six real networks, each chosen for what it proves.

| network          | chain_type     | proves                                                   |
| ethereum-mainnet | ethereum       | the reference, plus blob fees (EIP-4844)                 |
| optimism-mainnet | optimism       | L2 fee split, L1 deposits, L1 withdrawals                |
| gnosis-mainnet   | default        | another native currency (XDAI), the generic profile      |
| rootstock-mainnet| rsk            | an older EVM (legacy txs only), native RBTC              |
| celo-mainnet     | optimism-celo  | fees paid in a token instead of the native currency      |
| zksync-era       | zksync         | the scope's edge: a zkEVM, its L1 status and system calls |
"""
import re
from pathlib import Path

import pytest

from anychain.chains import ChainProfile, audit_fields
from anychain.config import load_config
from tests.conftest import ROOT, USDC_TX, mutated, replay_bundle

# config, fixture, tx hash: one real recorded transaction per network
NETWORK_CASES = [
    ("ethereum-mainnet", "eth_usdc_transfer", USDC_TX),
    ("optimism-mainnet", "op_usdc_transfer", "0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921"),
    ("gnosis-mainnet", "gnosis_transfer", "0xac36f2fa8f0fc93e51ee3bee4f40a96421de08aa5f515c4602cdeb4a1ec6303b"),
    ("rootstock-mainnet", "rootstock_transfer", "0x677c354e740f6bcbbd1ef6f4ea5ed0247c0fd711b85fc9a16798a07473756351"),
    ("celo-mainnet", "celo_fee_currency", "0x4ca38efad66cf866bbadc1c91ecd3d61c524c747de68eb196d28b882b4b86aca"),
    ("zksync-era", "zksync_processed_on_l2", "0xe212432a6d3f26d21c1240c1f376d605b173d6be0b2c2f01444d0d296e57a9c3"),
]


def _cfg(name):
    return load_config(str(ROOT / "configs" / f"{name}.yaml"))


def _texts(bundle):
    return " ".join(e.text for e in bundle.items)


def _gaps(bundle, what):
    return [g for g in bundle.gaps if g.what == what]


# ---- the same checks on every network ---------------------------------------------

@pytest.mark.parametrize("config,fixture,tx_hash", NETWORK_CASES, ids=[c[0] for c in NETWORK_CASES])
def test_every_network_gives_a_sourced_answer_in_its_own_terms(config, fixture, tx_hash):
    cfg = _cfg(config)
    b = replay_bundle(cfg, tx_hash, fixture)
    assert b.status == "success" and b.network == config
    assert b.items[0].sources[0].url == cfg.explorer.tx_url(tx_hash)  # links point at this network's explorer
    assert all(s.url or s.detail for e in b.items for s in e.sources)
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.data["token"] in (cfg.network.native_symbol, "USD₮")  # never another network's currency
    assert any(e.kind == "cross_check" and e.data["agrees"] for e in b.items)  # RPC confirms on every network
    assert not _gaps(b, "Chain type") and not _gaps(b, "Network-specific details")  # profile fits payload


@pytest.mark.parametrize("config", sorted(p.stem for p in (ROOT / "configs").glob("*.yaml") if "example" not in p.name))
def test_every_shipped_config_loads_with_a_dedicated_profile(config):
    from anychain.chains import PROFILES
    assert _cfg(config).network.chain_type in PROFILES


def test_no_network_specific_values_in_code():
    """Portability by config only: no explorer/RPC domains, chain ids, symbols or addresses in src/."""
    src = "\n".join(p.read_text() for p in (ROOT / "src" / "anychain").rglob("*.py"))
    # Not network values: Blockscout's docs link, the OpenAI API endpoint (an LLM provider, D27), and
    # source links for chain-type behaviour, which live in chain profiles only (D31).
    allowed = {"docs.blockscout.com", "api.openai.com"}
    domains = set(re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", src)) - allowed
    # Source links (github.com) for behaviour a rule relies on are allowed only where rules live, and
    # repo.py downloads configured GitHub repositories (the host, not a network value).
    rule_files = {"chains.py", "diagnosis.py", "repo.py"}
    domains -= {"github.com", "codeload.github.com"}
    elsewhere = "\n".join(p.read_text() for p in (ROOT / "src" / "anychain").rglob("*.py") if p.name not in rule_files)
    assert "github.com" not in elsewhere, "source links only in chains.py and diagnosis.py"
    assert not domains, f"hardcoded hosts: {domains}"
    assert not re.search(r"chain_id\s*(==|!=)\s*\d|chain_id\s+in\s*[\[({]\s*\d", src), "chain id literal"
    assert not re.findall(r"0x[0-9a-fA-F]{40}", src), "hardcoded address"
    symbols = {_cfg(p.stem).network.native_symbol for p in (ROOT / "configs").glob("*-*.yaml") if "example" not in p.name}
    for symbol in symbols:
        assert not re.search(rf"[\"']{symbol}[\"']", src), f"hardcoded currency {symbol}"


# ---- what each profile interprets --------------------------------------------------

def test_celo_fee_paid_in_token_is_not_called_celo():
    b = replay_bundle(_cfg("celo-mainnet"), NETWORK_CASES[4][2], "celo_fee_currency")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.text.startswith("Fee paid: 0.003604051072171875 USD₮, paid in USD₮ instead of CELO")


def test_optimism_fee_is_split_and_sums_to_the_total():
    from decimal import Decimal
    b = replay_bundle(_cfg("optimism-mainnet"), NETWORK_CASES[1][2], "op_usdc_transfer")
    fee = next(e for e in b.items if e.kind == "fee")
    parts = fee.data["parts"]
    assert set(parts) == {"L2 execution", "L1 data"}
    assert sum(Decimal(v) for v in parts.values()) == Decimal(fee.data["fee"])  # exact, not just "positive"
    assert "L1 data" in fee.text and "in total" in fee.text


def test_optimism_withdrawal_and_system_transaction_are_stated():
    cfg = _cfg("optimism-mainnet")
    w = replay_bundle(cfg, "0x300df618a29d14ade1e644d22f8ac134bcbe99ac64e0aa2ca2db3309522f8217", "op_withdrawal")
    assert "Started a withdrawal to L1" in _texts(w) and "'Ready to prove'" in _texts(w)
    # This fixture is the sequencer's L1 attributes transaction, not a user deposit: the
    # text must not claim it came from L1 through the bridge.
    d = replay_bundle(cfg, "0x00cadc051b2e3894e3f7c953db6eca56e6853e325a84b5ba25c4db1f80b14be5", "op_deposit")
    assert "L1 attributes transaction: the system transaction the sequencer puts first in every block" in _texts(d)
    assert "user deposit through the bridge" not in _texts(d)
    assert "(OP Stack depositor system account (sequencer, not a user))" in _texts(d)


def test_ethereum_blob_fee_is_added_to_the_execution_fee():
    b = replay_bundle(_cfg("ethereum-mainnet"), "0x77215ad174af1162134ff6f99bd9e118bf87ca2927db8e341aa394d7ce1728b6",
                      "eth_blob")
    fee = next(e for e in b.items if e.kind == "fee")
    # Real numbers: execution 97062677942394 wei + blob 10417524703232 wei.
    assert fee.data["fee"] == "0.000107480202645626"
    assert "Carried 2 blob(s)" in _texts(b)


def test_zksync_reports_its_l1_status_and_names_system_contracts():
    cfg = _cfg("zksync-era")
    final = replay_bundle(cfg, "0x669f390346545904cb53a9934f6140d054675fe5463f3dc71624bb3e4f351a21",
                          "zksync_executed_on_l1")
    assert "'Executed on L1' (batch 499177)" in _texts(final) and "executed in L1 transaction 0x7b254d92" in _texts(final)
    assert not _gaps(final, "L1 status from the node")  # the explorer named the L1 execution: nothing to confirm
    fresh = replay_bundle(cfg, NETWORK_CASES[5][2], "zksync_processed_on_l2")
    assert "the explorer lists no L1 transaction for it yet" in _texts(fresh)
    assert "not yet committed" not in _texts(fresh)  # the explorer lags, so it is never asserted
    # the node's answer was added to this recording later (2026-10-07): committed and proven, not executed
    assert "the explorer does not list the committed and proven steps yet" in _texts(fresh)
    assert "(zkSync bootloader: collects fees and pays refunds)" in _texts(fresh)


def test_zksync_l1_status_lagging_on_the_explorer_is_corrected_by_the_node():
    # Real tx: the explorer still said "Sealed on L2" with no L1 hashes on 2026-10-07, while the node
    # reported batch 517442 committed, proven and executed on L1 on 2026-09-19 (found by the acceptance review).
    b = replay_bundle(_cfg("zksync-era"), "0xb83b703477961ca98d2bad14f001590ed1d1afca81cbee5e3ad0353d9dfd793f",
                      "zksync_explorer_l1_behind")
    text = _texts(b)
    assert "'Sealed on L2' (batch 517442); the explorer lists no L1 transaction for it yet" in text
    assert "not yet committed" not in text
    assert ("The node reports this transaction committed in L1 transaction 0x13ad8527935" in text
            and "executed in L1 transaction 0x918d2b407ba8" in text
            and "the explorer does not list the committed, proven and executed steps yet" in text)
    node_fact = next(e for e in b.items if "The node reports" in e.text)
    assert node_fact.sources[0].kind == "rpc" and "zks_getTransactionDetails" in node_fact.sources[0].detail


# ---- wrong or missing chain_type ---------------------------------------------------

def test_wrong_chain_type_is_flagged():
    cfg = _cfg("celo-mainnet")
    cfg.network.chain_type = "default"
    b = replay_bundle(cfg, NETWORK_CASES[4][2], "celo_fee_currency")
    flagged = {g.why.split("typical of ")[1].split(" ")[0] for g in _gaps(b, "Chain type")}
    assert flagged == {"'optimism'", "'optimism-celo'"}
    assert "Fee paid: 0.003604051072171875 CELO" in _texts(b)  # without the profile the fee is read as native...
    assert _gaps(b, "Chain type")  # ...which is exactly why the mismatch is reported


def test_chain_type_without_profile_falls_back_and_says_so():
    cfg = _cfg("gnosis-mainnet")
    cfg.network.chain_type = "arbitrum"
    b = replay_bundle(cfg, NETWORK_CASES[2][2], "gnosis_transfer")
    assert b.status == "success"
    assert "no dedicated profile" in _gaps(b, "Network-specific details")[0].why


def test_unknown_structured_fields_are_declared():
    tx = {"hash": "0x1", "fee": {"value": "1"}, "arbitrum": {"batch_number": 7}, "brand_new_scalar": "x"}
    audit = audit_fields(tx, ChainProfile())
    assert audit.unknown == ["arbitrum"]  # a block of chain data is declared; a plain new field is not noise
    assert audit.other_types == {}


# ---- eighth review (Fable): real cases on the new networks -------------------------

def test_celo_adapter_fee_is_read_through_the_configured_adapter():
    # Circle's USDC fee adapter has no decimals() on-chain; config says its units (adapterDecimals = 18).
    tx = "0x6470963dc7a47fffeb6dee28d9ad31f6e78208be15c9919a8b193c589996404d"
    b = replay_bundle(_cfg("celo-mainnet"), tx, "celo_fee_token_no_decimals")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.text.startswith("Fee paid: 0.007822697203928125 USDC, paid in USDC instead of CELO")
    assert "Circle's USDC fee adapter" in fee.text and not _gaps(b, "Fee")


def test_unknown_fee_token_without_decimals_stays_raw_and_says_so():
    cfg = _cfg("celo-mainnet")
    cfg.fee_tokens = {}
    tx = "0x6470963dc7a47fffeb6dee28d9ad31f6e78208be15c9919a8b193c589996404d"
    b = replay_bundle(cfg, tx, "celo_fee_token_no_decimals")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.text.startswith("Fee paid: 7822697203928125 raw units of 0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B")
    assert any("decimals" in g.why for g in _gaps(b, "Fee"))


def test_revert_without_data_is_not_called_undecoded_data():
    tx = "0xf354bf88343e794ca6f4574416778ef41bd06a51f32fb5c1ddfc1e71404ab6d1"
    b = replay_bundle(_cfg("gnosis-mainnet"), tx, "gnosis_revert_no_data")
    assert b.status == "failed"
    assert "reverted without any revert data" in _texts(b) and "undecoded revert data" not in _texts(b)


def test_rootstock_reward_transaction_is_a_system_transaction():
    tx = "0xc56d7cb6e5acdadf71898c8a6c11a0c74d42d0d172eae3bb3b48d874ef521bee"
    b = replay_bundle(_cfg("rootstock-mainnet"), tx, "rootstock_remasc")
    assert "System transaction: sent from the zero address" in _texts(b)
    assert "Plain RBTC transfer" not in _texts(b)
    assert "(Rootstock REMASC (block rewards), native contract)" in _texts(b)


def test_unknown_chain_type_value_degrades_instead_of_refusing_to_load(tmp_path):
    cfg = _cfg("gnosis-mainnet")
    cfg.network.chain_type = "celo"  # pre-OP Celo explorers used this value
    b = replay_bundle(cfg, NETWORK_CASES[2][2], "gnosis_transfer")
    assert b.status == "success"
    assert "not a Blockscout CHAIN_TYPE value this tool knows" in _gaps(b, "Chain type")[0].why


# ---- the seven gaps from docs/CHAINS.md (real recordings unless stated) ------------

def test_gap1_native_currency_is_not_counted_twice_on_zksync():
    tx = "0x7839f1fd4d85a39d36a56ae7f3ee2988a20330d87480bf975c03a46592a117f4"
    b = replay_bundle(_cfg("zksync-era"), tx, "zksync_native_eth_send")
    assert not any(e.kind == "token_transfer" and " ETH from " in e.text for e in b.items)
    moves = [e for e in b.items if e.kind == "native_transfer"]
    assert len(moves) == 1 and "0.000073403258318442 ETH" in moves[0].text and "not a second asset" in moves[0].text


def test_gap1_native_currency_is_not_counted_twice_on_celo():
    tx = "0x64f5270ef3b298a6d2ddefdba4f2c83faab12123020136915b173aef0c5526f3"
    b = replay_bundle(_cfg("celo-mainnet"), tx, "celo_native_transfer")
    assert not any(e.kind == "token_transfer" for e in b.items)
    assert "Native CELO movement of 0.01278614 CELO" in _texts(b)


def test_gap5_zksync_fee_flow_nets_to_the_explorer_fee():
    tx = "0x7839f1fd4d85a39d36a56ae7f3ee2988a20330d87480bf975c03a46592a117f4"
    b = replay_bundle(_cfg("zksync-era"), tx, "zksync_native_eth_send")
    flow = next(e for e in b.items if e.kind == "fee_flow")
    assert flow.data["matches_explorer_fee"] and not flow.data["paymaster"]
    assert flow.data["prepaid"] - flow.data["refunded"] == flow.data["net"]


def test_gap5_zksync_paymaster_is_named():
    tx = "0x092a7ba32ffb022c9678385ee59d2de2d2192a4cceb2ca2618789874841603af"
    b = replay_bundle(_cfg("zksync-era"), tx, "zksync_paymaster")
    flow = next(e for e in b.items if e.kind == "fee_flow")
    assert flow.data["paymaster"] and flow.data["matches_explorer_fee"]
    assert "prepaid by 0xA2Aac7bC9725c36ad9B12D2407dF8de6B2B68359 (a paymaster), not by the sender" in flow.text
    # Real numbers: the sender sent 598.42 NODL to the paymaster and got 512.64 back.
    assert "the sender paid the paymaster 85.783676209796661425 NODL (net of what it returned)" in flow.text


def test_gap2_operator_fee_is_its_own_part():
    # Not seen live (OP Mainnet and Celo charge no operator fee today; Blockscout then omits the
    # field), so a real recording gets an operator_fee added.
    def with_operator_fee(body):
        body["operator_fee"] = "1000"
    cfg = _cfg("optimism-mainnet")
    overrides = mutated("op_usdc_transfer", NETWORK_CASES[1][2], with_operator_fee)
    b = replay_bundle(cfg, NETWORK_CASES[1][2], "op_usdc_transfer", overrides=overrides)
    fee = next(e for e in b.items if e.kind == "fee")
    assert set(fee.data["parts"]) == {"L2 execution", "L1 data", "operator fee"}
    from decimal import Decimal
    assert sum(Decimal(v) for v in fee.data["parts"].values()) == Decimal(fee.data["fee"])


def test_gap3_explorer_classification_separates_l1_attributes_from_user_deposits():
    cfg = _cfg("optimism-mainnet")
    user = replay_bundle(cfg, "0xffd7237981c26cac7cee1b2cdaf0d16279a423080a1cd7b492c89a83f01649bb", "op_user_deposit")
    assert "Usually a user deposit through the bridge" in _texts(user) and "L1 attributes" not in _texts(user)
    system = replay_bundle(cfg, "0x00cadc051b2e3894e3f7c953db6eca56e6853e325a84b5ba25c4db1f80b14be5", "op_deposit")
    assert "L1 attributes transaction" in _texts(system) and "user deposit through" not in _texts(system)


def test_gap3_rootstock_classification_and_gap6_native_contract():
    tx = "0x156c82cc49e90e374b87d7080f7866c4fe273ce74f99c9d5713c1023a6f0f556"
    b = replay_bundle(_cfg("rootstock-mainnet"), tx, "rootstock_bridge")
    assert "classifies this as a Rootstock bridge transaction" in _texts(b)
    assert not any("verified contract" in g.needed or "verified on the explorer" in g.needed for g in b.gaps)
    assert any("native contract built into the node" in g.why and "published ABI" in g.needed for g in b.gaps)


def test_gap6_call_to_native_contract_is_not_asked_to_be_verified():
    to = "0x0000000000000000000000000000000001000006"
    def call_bridge(body):
        body["raw_input"] = "0x12345678"
    tx = "0x156c82cc49e90e374b87d7080f7866c4fe273ce74f99c9d5713c1023a6f0f556"
    b = replay_bundle(_cfg("rootstock-mainnet"), tx, "rootstock_bridge", overrides=mutated("rootstock_bridge", tx, call_bridge))
    call = next(e for e in b.items if e.kind == "call")
    assert "a contract built into the network's node" in call.text and to in call.text
    assert "no source code or ABI" not in call.text  # false: the Bridge's ABI is published
    gap = _gaps(b, "Call decoding")[0]
    assert "published ABI" in gap.needed and "verified contract" not in gap.needed
    assert not _gaps(b, "Code at execution time")


def test_gap7_zksync_priority_type_is_stated():
    tx = "0xd44240d0afcdf47a8bb91ed59abb1b273e749e83d5b5cac514f7dda802c3b0e4"
    b = replay_bundle(_cfg("zksync-era"), tx, "zksync_priority_l1")
    assert "Priority transaction (zkSync type 255): submitted through L1" in _texts(b)


def test_gap7_post_exec_and_interop_are_declared():
    # Not seen live: real recording with the two fields Blockscout would add.
    def future(body):
        body["transaction_types"] = list(body.get("transaction_types") or []) + ["op_stack_post_exec_transaction"]
        body["op_interop_messages"] = [{"nonce": 1}, {"nonce": 2}]
    cfg = _cfg("optimism-mainnet")
    b = replay_bundle(cfg, NETWORK_CASES[1][2], "op_usdc_transfer", overrides=mutated("op_usdc_transfer", NETWORK_CASES[1][2], future))
    assert "post-execution transaction (type 0x7D)" in _texts(b)
    assert "2 cross-chain interop message(s)" in _texts(b)
    assert not _gaps(b, "Network-specific details")  # op_interop_messages is a known OP field now


# ---- review r10: native value counted once across all fact kinds -------------------

def test_internal_native_send_points_to_the_native_movement_on_celo():
    tx = "0x6be4971d7a629bcbdcd14ed45426370359c4cf2a8205860ba371ea1f48b39429"
    b = replay_bundle(_cfg("celo-mainnet"), tx, "celo_faucet_internal_celo")
    moves = {e.data["value"]: e.id for e in b.items if e.kind == "native_transfer"}
    internal = [e for e in b.items if e.kind == "internal_call" and e.data.get("moves_value")]
    assert len(internal) == 2 and all(e.data["same_as"] == moves[int(e.data["value"])] for e in internal)
    assert all("the same movement as E" in e.text for e in internal)


def test_zksync_user_calls_are_not_hidden_behind_system_calls():
    tx = "0x7839f1fd4d85a39d36a56ae7f3ee2988a20330d87480bf975c03a46592a117f4"
    b = replay_bundle(_cfg("zksync-era"), tx, "zksync_native_eth_send")
    assert "between the network's system contracts not listed" in _texts(b)
    send = [e for e in b.items if e.kind == "internal_call" and e.data.get("value") == "73403258318442"]
    assert send and send[0].data["same_as"]  # the user's ETH send, visible and linked to its native movement
    assert not _gaps(b, "Internal calls")  # nothing cut by the 30-call limit any more


# ---- every branch of the node's L1 confirmation, from the real node answer ---------------

L1_TX = "0xb83b703477961ca98d2bad14f001590ed1d1afca81cbee5e3ad0353d9dfd793f"


def _node_answer():
    from tests.conftest import recorded_body
    return recorded_body("zksync_explorer_l1_behind", f'zks_getTransactionDetails ["{L1_TX}"]')["result"]


def _l1(explorer: dict, node):
    from anychain.chains import ZkSyncProfile
    tx = {"zksync": {"status": "Sealed on L2", "batch_number": 517442, **explorer}}
    return ZkSyncProfile().node_facts(L1_TX, tx, lambda _m, _p: node)


def test_l1_same_steps_on_both_sources_is_not_called_behind():
    node = _node_answer()
    node["ethExecuteTxHash"] = None
    [fact] = _l1({"commit_transaction_hash": node["ethCommitTxHash"],
                  "prove_transaction_hash": node["ethProveTxHash"]}, node)
    assert "the same L1 steps the explorer lists" in fact.text and "not list" not in fact.text


def test_l1_only_the_missing_step_is_named():
    node = _node_answer()
    [fact] = _l1({"commit_transaction_hash": node["ethCommitTxHash"]}, node)
    assert "the explorer does not list the proven and executed steps yet" in fact.text


def test_l1_explorer_ahead_of_the_node_is_a_gap_not_a_fact():
    from anychain.chains import ChainGap
    node = _node_answer()
    commit = node["ethCommitTxHash"]
    for key in ("ethCommitTxHash", "ethProveTxHash", "ethExecuteTxHash"):
        node[key] = None
    [gap] = _l1({"commit_transaction_hash": commit}, node)
    assert isinstance(gap, ChainGap) and gap.cause == "source_behind" and "disagree" in gap.why


def test_l1_different_hash_for_a_step_is_a_gap():
    from anychain.chains import ChainGap
    node = _node_answer()
    [gap] = _l1({"commit_transaction_hash": "0x" + "ab" * 32}, node)
    assert isinstance(gap, ChainGap)


def test_l1_neither_source_has_a_step():
    node = _node_answer()
    for key in ("ethCommitTxHash", "ethProveTxHash", "ethExecuteTxHash"):
        node[key] = None
    node["status"] = "included"
    [fact] = _l1({}, node)
    assert fact.text == "The node reports no L1 transaction for it yet either (node status 'included')."


def test_l1_unknown_to_the_node_is_a_source_error_not_our_defect():
    from anychain.collectors.http import CollectorError
    with pytest.raises(CollectorError) as err:
        _l1({}, None)
    assert err.value.retryable is False  # becomes a source_error gap via _safely


def test_l1_no_explorer_claim_means_no_node_call():
    from anychain.chains import ZkSyncProfile
    calls = []
    assert ZkSyncProfile().node_facts(L1_TX, {}, lambda m, p: calls.append(m)) == []
    assert ZkSyncProfile().node_facts(L1_TX, {"zksync": {"status": "Executed on L1", "execute_transaction_hash": "0x1"}},
                                      lambda m, p: calls.append(m)) == []
    assert calls == []


def test_l1_node_on_the_wrong_chain_is_not_asked():
    cfg = _cfg("zksync-era").model_copy(deep=True)
    cfg.network.chain_id = 1
    b = replay_bundle(cfg, L1_TX, "zksync_explorer_l1_behind")
    assert not [e for e in b.items if "The node reports" in e.text]
    assert not _gaps(b, "L1 status from the node")


# ---- PHASE2 T4: one real failed transaction per diagnosis rule -------------------------------------

DIAGNOSIS_CASES = [
    # fixture, network, rule, level, words the finding must say
    ("celo_fail_balance_confirmed", "celo-mainnet", "insufficient_balance", "confirmed",
     ["Cause confirmed", "was 1445 raw units (0.001445 USD₮), less than the 106500 raw units (0.1065 USD₮)"]),
    ("rootstock_fail_paused", "rootstock-mainnet", "paused", "confirmed", ["paused() returned true"]),
    ("op_fail_deadline", "optimism-mainnet", "deadline", "candidate",  # unverified contract: words only
     ["Possible cause", "'block number deadline'"]),
    ("gnosis_fail_all_gas", "gnosis-mainnet", "out_of_gas", "candidate",  # the trace names the inner call (D50)
     ["An internal call ran out of gas", "'Out of gas'", "293252 of its 293668"]),
    ("celo_fail_execution_reverted_no_data", "celo-mainnet", "no_reason", "single_source",
     ["No reason is available"]),  # the explorer's "execution reverted" is not a reason
    ("zksync_fail_generic", "zksync-era", "generic_failure", "single_source", ["carries no reason"]),
    ("rootstock_fail_slippage", "rootstock-mainnet", "slippage", "single_source", ["'Too much requested'"]),
    ("gnosis_fail_contract_reason", "gnosis-mainnet", "contract_reason", "single_source", ["'NOT_YET'"]),
    ("eth_failed_unverified_bot", "ethereum-mainnet", "slippage", "candidate", ["Possible cause"]),
    ("gnosis_revert_no_data", "gnosis-mainnet", "no_reason", "single_source", ["No reason is available"]),
]


@pytest.mark.parametrize("fixture, network, rule, level, words", DIAGNOSIS_CASES)
def test_diagnosis_on_real_failures(fixture, network, rule, level, words):
    from tests.test_golden import _case
    _config, tx = _case(fixture)
    b = replay_bundle(_cfg(network), tx, fixture)
    [finding] = [e for e in b.items if e.kind == "diagnosis" and not e.data.get("from_replay")]
    assert (finding.data["rule"], finding.data["level"], finding.confidence) == (rule, level, level)
    for w in words:
        assert w in finding.text, (w, finding.text)
    assert finding.data["next_steps"]["support"] and finding.data["next_steps"]["developer"]
    assert "Next step for a non-technical reader:" in finding.text and "Next step for a developer:" in finding.text
    for read_id in finding.data["reads"]:  # every read it relies on is a fact from the node
        read = next(e for e in b.items if e.id == read_id)
        assert read.kind == "state_read" and read.confidence == "confirmed"


def test_reused_slippage_words_without_a_swap_call_are_only_a_candidate():
    from anychain.collectors.types import RevertReason
    from anychain.diagnosis import Context, diagnose
    reason = RevertReason.from_api({"method_call": "Error(string reason)",
                                    "parameters": [{"name": "reason", "value": "Too much requested"}]})
    ctx = Context(reason=reason, result="Reverted", call={"function": "execute", "args": {}}, sender="0x1", to="0x2",
                  to_text="0x2", block=10, gas_used=1, gas_limit=10, reader=None)
    assert diagnose(ctx).level == "candidate"


def test_the_node_not_keeping_old_state_is_a_gap_not_a_cause():
    # The balance read needs the parent block's state; when the node refuses it, the finding stays
    # on the explorer's reason and a gap says what is missing.
    from tests.test_golden import _case
    _config, tx = _case("celo_fail_balance_confirmed")
    refusal = {"status": 403, "body": '{"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a '
                                      'personal token."},"id":1}'}
    b = replay_bundle(_cfg("celo-mainnet"), tx, "celo_fail_balance_confirmed",
                      overrides={f'"{hex(78962883)}"]': refusal})  # the balance read at the parent block
    [finding] = [e for e in b.items if e.kind == "diagnosis" and not e.data.get("from_replay")]
    assert finding.data["level"] == "single_source" and "Cause confirmed" not in finding.text
    assert any(g.what == "Diagnosis" and "could not be read" in g.why for g in b.gaps)



# ---- PHASE2 T5: re-run the call when the explorer gives no reason -----------------------------------

def _replay_case(fixture, network, **kw):
    from tests.test_golden import _case
    _config, tx = _case(fixture)
    b = replay_bundle(_cfg(network), tx, fixture, **kw)
    replays = [e for e in b.items if e.kind == "replay"]
    from_replay = [e for e in b.items if e.kind == "diagnosis" and e.data.get("from_replay")]
    return b, replays, from_replay


def test_replay_finds_a_reason_the_explorer_did_not_give():
    # zkSync stores "Bootloader-based tx failed" for every failure; the replay reverts with "Too little received".
    b, [replay], [finding] = _replay_case("zksync_fail_replay_reason", "zksync-era")
    assert replay.confidence == "confirmed" and "'Too little received'" in replay.text
    assert "may differ from what happened" in replay.text  # its limits are stated
    assert finding.confidence == "candidate" and "slippage" in finding.text  # never more than a candidate
    assert not finding.text.count("Possible cause") > 1


def test_replay_custom_error_without_abi_is_named_and_gapped():
    b, [replay], from_replay = _replay_case("zksync_fail_generic", "zksync-era")
    assert "0x40206e43" in replay.text and from_replay == []  # a selector alone says nothing about the cause
    assert any(g.what == "Replay" and g.cause == "not_interpretable" for g in b.gaps)


@pytest.mark.parametrize("fixture, network, words", [
    ("celo_fail_execution_reverted_no_data", "celo-mainnet", "did not revert. That is inconclusive"),
    ("gnosis_revert_no_data", "gnosis-mainnet", "reverted with no revert data"),
])
def test_replay_without_a_reason_adds_no_cause(fixture, network, words):
    b, [replay], from_replay = _replay_case(fixture, network)
    assert words in replay.text and from_replay == []


def test_no_replay_when_the_explorer_gave_a_reason():
    b, replays, _ = _replay_case("celo_fail_balance_confirmed", "celo-mainnet")
    assert replays == []


def test_a_node_that_refuses_the_replay_is_a_gap():
    from tests.test_golden import _case
    _config, tx = _case("gnosis_revert_no_data")
    refusal = {"status": 403, "body": '{"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a '
                                      'personal token."},"id":1}'}
    b, replays, _ = _replay_case("gnosis_revert_no_data", "gnosis-mainnet",
                                 overrides={f'"{hex(48639299)}"]': refusal})
    assert replays == [] and any(g.what == "Replay" and g.cause == "source_error" for g in b.gaps)


# ---- cases from the clean-context review of T5 (written before the fixes) ---------------------------

def _gnosis_no_data(overrides=None):
    from tests.test_golden import _case
    _config, tx = _case("gnosis_revert_no_data")
    return tx, replay_bundle(_cfg("gnosis-mainnet"), tx, "gnosis_revert_no_data", overrides=overrides)


def _explorer_tx_override(change):
    from tests.conftest import mutated
    from tests.test_golden import _case
    _config, tx = _case("gnosis_revert_no_data")
    return mutated("gnosis_revert_no_data", f"/transactions/{tx}", change)


def test_replay_limits_say_nodes_check_the_value():
    from anychain.reads import REPLAY_LIMITS
    assert "skip the sender's balance check" not in REPLAY_LIMITS and "value" in REPLAY_LIMITS


def test_replay_without_the_senders_value_is_its_own_gap():
    # Ethereum/Gnosis/OP nodes answer -32003 "EVM error: OutOfFunds" (seen live by the review)
    body = '{"jsonrpc":"2.0","id":1,"error":{"code":-32003,"message":"EVM error: OutOfFunds"}}'
    tx, b = _gnosis_no_data({f'"{hex(48639299)}"]': {"status": 200, "body": body}})
    [gap] = [g for g in b.gaps if g.what == "Replay"]
    assert gap.cause == "not_interpretable" and "did not hold the value" in gap.why
    assert not [e for e in b.items if e.kind == "replay"]


@pytest.mark.parametrize("change, words", [
    (lambda body: body.update(type=126), "deposit"),                                         # OP deposit 0x7e
    (lambda body: body.update(authorization_list=[{"address_hash": "0x" + "1" * 40, "authority": "0x" + "2" * 40,
                                                   "status": "ok", "nonce": 1, "chain_id": 100}]), "EIP-7702"),
    (lambda body: body.update(to=None), "creation"),
])
def test_replay_is_skipped_when_it_cannot_reproduce_the_call(change, words):
    tx, b = _gnosis_no_data(_explorer_tx_override(change))
    assert not [e for e in b.items if e.kind == "replay"]
    assert any(g.what == "Replay" and words in g.why for g in b.gaps), [g.why for g in b.gaps]


def test_replay_uses_the_nodes_transaction_including_gas():
    from tests.conftest import make_transport
    from tests.test_golden import _case
    _config, tx = _case("gnosis_revert_no_data")
    transport = make_transport("gnosis_revert_no_data", None, None)
    b = replay_bundle(_cfg("gnosis-mainnet"), tx, "gnosis_revert_no_data", transport=transport)
    [call] = [c for c in transport.calls if "eth_call" in c]
    assert '"gas"' in call
    [replay] = [e for e in b.items if e.kind == "replay"]
    assert replay.confidence == "confirmed" and all(s.kind == "rpc" for s in replay.sources)


def test_replay_finding_speaks_of_the_replay_not_the_transaction():
    b, _replays, [finding] = _replay_case("zksync_fail_replay_reason", "zksync-era")
    assert "the replay reverted with" in finding.text and "when the transaction ran" not in finding.text


@pytest.mark.parametrize("data, kind", [
    ("0x08c379a0", "malformed"), ("0x4e487b71", "malformed"), ("0x0102", "malformed"),
])
def test_malformed_revert_data_is_not_called_a_custom_error(data, kind):
    from anychain.decoder import decode_revert
    d = decode_revert(data)
    assert d.kind == kind and "custom error" not in d.text


# ---- acceptance 2026-10-08: a blob transaction on Gnosis (D54) ----

GNOSIS_BLOB_TX = "0xda8a55bc9aff11d9e0c349c3d71756d9bce5796f3df87183cdce94137f867da2"


def test_gnosis_blob_fee_is_part_of_the_fee():
    # real: the explorer's fee is the execution part (992205000198441); burnt_blob_fee 131072000000000 equals the
    # node's blobGasUsed 0x20000 * blobGasPrice 0x3b9aca00; the node's total is 1123277000198441
    b = replay_bundle(_cfg("gnosis-mainnet"), GNOSIS_BLOB_TX, "gnosis_blob_fee")
    fee = next(e for e in b.items if e.kind == "fee")
    assert fee.data["fee"] == "0.001123277000198441" and not _gaps(b, "Chain type")


def test_a_blob_fee_the_profile_does_not_add_is_never_shown_as_the_whole_fee():
    cfg = _cfg("gnosis-mainnet")
    cfg = cfg.model_copy(update={"network": cfg.network.model_copy(update={"chain_type": "default"})})
    b = replay_bundle(cfg, GNOSIS_BLOB_TX, "gnosis_blob_fee")
    fee = next(e for e in b.items if e.kind == "fee")
    assert "may not be the whole fee" in fee.text
    assert any("131072000000000" in g.why for g in _gaps(b, "Fee"))
