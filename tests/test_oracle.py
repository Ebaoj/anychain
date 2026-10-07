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
