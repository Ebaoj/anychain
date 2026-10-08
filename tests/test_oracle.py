"""The acceptance oracle (independent checks against the node), run on every recorded transaction.

Every recording must pass or be skipped with a reason: a failure here means the tool states something
the node contradicts, or the oracle itself is wrong. Either way it must be looked at."""
import json

import pytest

from anychain.config import load_config
from anychain.oracle import check_answer, receipt_transfers
from tests.conftest import FIXTURES, ROOT, replay_bundle
from tests.test_golden import FIXTURE_NAMES, _case


def _answer_and_node(fixture):
    config, tx_hash = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    records = json.loads((FIXTURES / f"{fixture}.json").read_text())
    offline = {cfg.explorer.base_url.split("//")[1]} if "rpc_only" in fixture else None
    bundle = replay_bundle(cfg, tx_hash, fixture, offline_hosts=offline)

    def node(method):
        for key, rec in records.items():
            if f'{method} ["{tx_hash}"]' in key and rec.get("body"):
                return json.loads(rec["body"]).get("result")
        return None

    corpus = "\n".join(rec.get("body", "") for rec in records.values())
    return bundle, cfg, node("eth_getTransactionByHash"), node("eth_getTransactionReceipt"), corpus


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_recorded_answer_passes_the_independent_checks(fixture):
    bundle, cfg, tx, receipt, corpus = _answer_and_node(fixture)
    failures = [c for c in check_answer(bundle, cfg, tx, receipt, corpus) if c.status == "fail"]
    assert not failures, [f"{c.name}: {c.detail}" for c in failures]


def test_oracle_catches_a_wrong_token_amount():
    bundle, cfg, tx, receipt, corpus = _answer_and_node("eth_usdc_transfer")
    transfer = next(e for e in bundle.items if e.kind == "token_transfer")
    transfer.data["value"] = transfer.data["value"] + 1
    assert any(c.name == "token_transfers" and c.status == "fail" for c in check_answer(bundle, cfg, tx, receipt, corpus))


def test_oracle_catches_a_wrong_fee_and_status():
    bundle, cfg, tx, receipt, corpus = _answer_and_node("eth_usdc_transfer")
    next(e for e in bundle.items if e.kind == "fee").data["fee"] = "0.1"
    bundle.status = "failed"
    statuses = {c.name: c.status for c in check_answer(bundle, cfg, tx, receipt, corpus)}
    assert statuses["fee"] == "fail" and statuses["status"] == "fail"


def test_oracle_catches_an_invented_address():
    bundle, cfg, tx, receipt, corpus = _answer_and_node("eth_usdc_transfer")
    bundle.items[0].text += " Also sent to 0x1111111111111111111111111111111111111111."
    assert any(c.name == "addresses_exist" and c.status == "fail" for c in check_answer(bundle, cfg, tx, receipt, corpus))


def test_receipt_transfer_decoding_covers_weth_deposits():
    _, _, _, receipt, _ = _answer_and_node("eth_uniswap_v2_swap")
    kinds = receipt_transfers(receipt, set())
    assert any(frm == "0x" + "0" * 40 for _, frm, _, _ in kinds)  # WETH Deposit shown as a mint


def test_wrap_with_both_deposit_and_transfer_counts_once():
    # Real shape (zkSync tx 0xe21abb35...): one wrap emits Deposit and Transfer(0x0 -> dst).
    from anychain.oracle import TRANSFER, WETH_DEPOSIT
    dst, amount = "0x" + "6e" * 20, "0x" + format(12512015561371302, "064x")
    topic = lambda a: "0x" + "0" * 24 + a[2:]
    receipt = {"logs": [
        {"address": "0xweth", "topics": [WETH_DEPOSIT, topic(dst)], "data": amount},
        {"address": "0xweth", "topics": [TRANSFER, topic("0x" + "0" * 40), topic(dst)], "data": amount},
    ]}
    assert len(receipt_transfers(receipt, set())) == 1


def test_token_paid_fee_is_checked_against_the_node():
    # Celo CIP-64: gasUsed * effectiveGasPrice is the fee in the fee token's (adapter's) units.
    bundle, cfg, tx, receipt, corpus = _answer_and_node("celo_fee_currency")
    fee = {c.name: c for c in check_answer(bundle, cfg, tx, receipt, corpus, fee_token_decimals=18)}["fee"]
    assert fee.status == "pass"
    next(e for e in bundle.items if e.kind == "fee").data["fee"] = "0.0036"
    fee = {c.name: c for c in check_answer(bundle, cfg, tx, receipt, corpus, fee_token_decimals=18)}["fee"]
    assert fee.status == "fail"



L1 = {"steps": {"committed": "0x13ad85", "proven": "0x8e6caf", "executed": "0x918d2b"},
      "times": {"committed": 100, "proven": 200, "executed": 300}}
EXPLORER_NONE = ("Status on L1, as reported by the explorer: 'Sealed on L2'; the explorer lists no L1 transaction "
                 "for it yet.", {"l1_status": "Sealed on L2"})


def _zk_answer(*facts):
    from anychain.models import EvidenceBundle
    b = EvidenceBundle(network="zksync-era", tx_hash="0x1", status="success")
    for text, data in facts:
        b.add("chain", text, [], data)
    return b


def _node(steps: dict, status="verified"):
    text = "The node reports this transaction " + ", ".join(f"{k} in L1 transaction {h}" for k, h in steps.items())
    return text, {"node_l1_status": status, **steps}


def test_l1_denial_of_existing_steps_fails():
    from anychain.oracle import _check_l1_status
    old = _zk_answer(("Status on L1, as reported by the explorer: 'Sealed on L2'; not yet committed to L1.",
                      {"l1_status": "Sealed on L2"}))
    assert _check_l1_status(old, L1).status == "fail"
    either = _zk_answer(EXPLORER_NONE, ("The node reports no L1 transaction for it yet either (node status "
                                        "'included').", {"node_l1_status": "included"}))
    assert _check_l1_status(either, L1).status == "fail"


def test_l1_every_existing_step_must_be_stated():
    from anychain.oracle import _check_l1_status
    assert _check_l1_status(_zk_answer(EXPLORER_NONE, _node({"committed": "0x13ad85"})), L1).status == "fail"
    full = _zk_answer(EXPLORER_NONE, _node(dict(L1["steps"])))
    assert _check_l1_status(full, L1).status == "pass"


def test_l1_wrong_label_or_extra_step_fails():
    from anychain.oracle import _check_l1_status
    swapped = _zk_answer(EXPLORER_NONE, _node({"committed": "0x8e6caf", "proven": "0x13ad85", "executed": "0x918d2b"}))
    assert _check_l1_status(swapped, L1).status == "fail"
    not_yet = {"steps": {"committed": "0x13ad85"}, "times": {"committed": 100}}
    extra = _zk_answer(("Status on L1, as reported by the explorer: 'Executed on L1'; committed in L1 transaction "
                        "0x13ad85, executed in L1 transaction 0xdead.",
                        {"l1_status": "Executed on L1", "committed": "0x13ad85", "executed": "0xdead"}))
    assert _check_l1_status(extra, not_yet).status == "fail"


def test_l1_steps_after_the_answer_are_not_held_against_it():
    from anychain.oracle import _check_l1_status
    early = _zk_answer(EXPLORER_NONE, _node({"committed": "0x13ad85"}))
    assert _check_l1_status(early, L1, answered_at=150).status == "pass"
    nothing_yet = _zk_answer(EXPLORER_NONE, ("The node reports no L1 transaction for it yet either (node status "
                                             "'included').", {"node_l1_status": "included"}))
    assert _check_l1_status(nothing_yet, L1, answered_at=50).status == "pass"


def test_l1_no_claim_or_no_oracle_is_a_skip_and_a_declared_gap_passes():
    from anychain.models import EvidenceBundle
    from anychain.oracle import _check_l1_status
    rpc_only = EvidenceBundle(network="zksync-era", tx_hash="0x1", status="success")
    rpc_only.add("overview", "(From RPC only) Transaction succeeded.", [])
    assert _check_l1_status(rpc_only, L1).status == "skip"
    assert _check_l1_status(_zk_answer(EXPLORER_NONE), None).status == "skip"
    for what, cause in (("L1 status from the node", "source_unavailable"), ("RPC transaction data", "config_error")):
        declared = _zk_answer(EXPLORER_NONE)
        declared.add_gap(what, "could not ask the node", "Try again", False, cause)
        assert _check_l1_status(declared, L1).status == "pass"
