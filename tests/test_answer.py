"""PHASE2_5 T6 (R4): the structured answer of the original plan (`explain --json`)."""
import json

from typer.testing import CliRunner

from anychain import cli
from anychain.answer import structured_answer
from anychain.config import load_config
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

KEYS = {"schema", "network", "tx_hash", "status", "mode", "summary", "summary_status", "calls", "transfers", "events",
        "diagnosis", "next_steps", "next_steps_by_reader", "security_notes", "confidence", "sources", "gaps",
        "evidence"}


def _bundle(fixture, config):
    _c, tx = _case(fixture)
    return replay_bundle(load_config(str(ROOT / "configs" / f"{config}.yaml")), tx, fixture)


def test_a_real_failure_in_the_plans_shape():
    b = _bundle("celo_fail_balance_confirmed", "celo-mainnet")
    a = structured_answer(b, summary=None, summary_status="skipped", mode="support")
    assert set(a) == KEYS and a["status"] == "failed" and a["summary"] is None
    [d] = [d for d in a["diagnosis"] if not d["from_replay"]]
    assert d["label"] == "CONFIRMED" and d["rule"] == "insufficient_balance"
    assert d["id"] in d["facts"] and all(f.startswith("E") for f in d["facts"])  # cited fact ids
    assert a["next_steps"] == a["next_steps_by_reader"]["support"] != a["next_steps_by_reader"]["developer"]
    assert a["confidence"] == {"status": "single_source", "status_basis": "explorer_only",
                               "diagnosis": "CONFIRMED"}  # recorded without the receipt
    assert {c["function"] for c in a["calls"] if c["top_level"]} == {"transfer"}
    assert all(s["url"] and s["facts"] for s in a["sources"])  # every source cited by at least one fact
    assert [e["id"] for e in a["evidence"]] == [e.id for e in b.items]


def test_a_real_success_has_no_diagnosis_and_lists_transfers_and_events():
    a = structured_answer(_bundle("eth_uniswap_v2_swap", "ethereum-mainnet"), None, "skipped", "developer")
    assert a["status"] == "success" and a["diagnosis"] == [] and a["next_steps"] == []
    assert a["confidence"]["diagnosis"] is None
    usdc = structured_answer(_bundle("eth_usdc_transfer", "ethereum-mainnet"), None, "skipped", "support")
    assert usdc["confidence"]["status"] == "confirmed"  # the node's receipt agrees with the explorer
    assert a["transfers"] and all(t["kind"] in ("token_transfer", "native_transfer", "transaction_value",
                                                "internal_value") for t in a["transfers"])
    assert a["events"] and all(e["event"] for e in a["events"])


def test_security_notes_and_gaps_are_carried():
    a = structured_answer(_bundle("eth_swaprouter02_multicall", "ethereum-mainnet"), None, "skipped", "auditor")
    assert a["security_notes"] and a["security_notes"][0]["notes"][0]["pattern"] == "delegatecall"
    b = replay_bundle(load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml")), _case("eth_usdc_transfer")[1],
                      "eth_usdc_transfer", offline_hosts={"ethereum-rpc.publicnode.com"})
    a = structured_answer(b, None, "skipped", "support")
    assert a["gaps"] and a["gaps"][0]["cause"] == "source_unavailable"
    assert a["confidence"]["status"] == "single_source"  # no node to agree


def test_the_cli_prints_it_and_keeps_the_bundle_behind_a_flag(monkeypatch):
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    args = ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml"), "--no-llm"]
    a = json.loads(CliRunner().invoke(cli.app, args + ["--json"]).output)
    assert a["schema"] == "anychain.answer/1" and a["summary_status"] == "skipped"
    bundle = json.loads(CliRunner().invoke(cli.app, args + ["--evidence"]).output)
    assert set(bundle) >= {"items", "gaps", "abi_sources"}


def test_the_written_summary_goes_in_when_the_model_writes_it(monkeypatch):
    from anychain.writer import CheckedAnswer
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: CheckedAnswer("It failed [E1].", "ok", []))
    out = CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml"),
                                       "--json", "--mode", "developer"]).output
    a = json.loads(out)
    assert a["summary"] == "It failed [E1]." and a["summary_status"] == "ok" and a["mode"] == "developer"
    assert a["next_steps"] == a["next_steps_by_reader"]["developer"]


# ---- review of T6 ----

def test_native_value_sent_and_moved_inside_is_in_transfers():
    a = structured_answer(_bundle("eth_uniswap_v2_swap", "ethereum-mainnet"), None, "skipped", "developer")
    kinds = {t["kind"] for t in a["transfers"]}
    assert {"transaction_value", "internal_value", "token_transfer"} <= kinds
    [sent] = [t for t in a["transfers"] if t["kind"] == "transaction_value"]
    assert sent["data"]["value"] == "0.2"
    assert any(t["data"]["value"] == "200000000000000000" for t in a["transfers"] if t["kind"] == "internal_value")
    bridge = structured_answer(_bundle("rootstock_bridge", "rootstock-mainnet"), None, "skipped", "support")
    assert bridge["transfers"]  # the value sent to the bridge


def test_a_reverted_transaction_lists_no_value_as_moved():
    a = structured_answer(_bundle("eth_fail_expired_v2", "ethereum-mainnet"), None, "skipped", "support")
    assert not [t for t in a["transfers"] if t["kind"] in ("transaction_value", "internal_value")]  # 0.045 ETH reverted


def test_a_failure_without_a_conclusion_is_unknown_with_steps():
    b = _bundle("celo_fail_balance_confirmed", "celo-mainnet")
    b.items = [e for e in b.items if e.kind != "diagnosis"]
    a = structured_answer(b, None, "skipped", "support")
    assert a["confidence"]["diagnosis"] == "UNKNOWN" and a["next_steps"]


def test_a_conclusion_lists_the_reason_and_the_call_it_rests_on():
    a = structured_answer(_bundle("eth_fail_expired_v2", "ethereum-mainnet"), None, "skipped", "support")
    [d] = a["diagnosis"]
    kinds = {e["id"]: e["kind"] for e in a["evidence"]}
    assert {"revert", "call", "diagnosis"} <= {kinds[f] for f in d["facts"]}


def test_summary_status_names_and_flag_conflicts(monkeypatch):
    from anychain.writer import CheckedAnswer
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: CheckedAnswer(None, "fail", ["x"]))
    args = ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml")]
    result = CliRunner().invoke(cli.app, args + ["--json"])
    assert json.loads(result.stdout)["summary_status"] == "withheld"
    both = CliRunner().invoke(cli.app, args + ["--json", "--evidence"])
    assert both.exit_code != 0 and "either" in both.output.lower()


def test_the_status_confidence_says_what_it_rests_on():
    a = structured_answer(_bundle("eth_usdc_transfer", "ethereum-mainnet"), None, "skipped", "support")
    assert a["confidence"]["status_basis"] == "node_and_explorer_agree"
    _c, tx = _case("eth_usdc_rpc_only")
    b = replay_bundle(load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml")), tx, "eth_usdc_rpc_only",
                      offline_hosts={"eth.blockscout.com"})
    assert structured_answer(b, None, "skipped", "support")["confidence"]["status_basis"] == "node_only"
