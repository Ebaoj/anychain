"""PHASE2_5 T5 (R7, R8): next steps for each reader, and the CONFIRMED / LIKELY / UNKNOWN label of every conclusion."""
import json

import pytest

from anychain.config import load_config
from anychain.diagnosis import RULES, SUPPORT_STEPS, label_for
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import CONFIG_BY_PREFIX, _case

GOLDEN = ROOT / "tests" / "golden"


@pytest.mark.parametrize("rule, level, label", [
    ("insufficient_balance", "confirmed", "CONFIRMED"),  # a read proves it
    ("deadline", "single_source", "CONFIRMED"),  # the decoded reason states it
    ("contract_reason", "single_source", "CONFIRMED"),
    ("access_control", "single_source", "CONFIRMED"),
    ("slippage", "candidate", "LIKELY"),  # a pattern suggests it
    ("possibly_out_of_gas", "candidate", "LIKELY"),
    ("replay", "candidate", "LIKELY"),  # the replay suggests it
    ("no_reason", "single_source", "UNKNOWN"),  # nothing to infer from
    ("generic_failure", "single_source", "UNKNOWN"),
])
def test_label_mapping(rule, level, label):
    assert label_for(rule, level) == label


def test_every_rule_has_steps_for_a_non_technical_reader():
    rules = {r.__name__.lstrip("_") for r in RULES}
    names = {"balance": "insufficient_balance", "allowance": "insufficient_allowance", "access": "access_control",
             "all_gas_no_reason": "possibly_out_of_gas", "router_pull": "router_transfer_from",
             "inner_origin": "out_of_gas"}
    for rule in rules:
        steps = SUPPORT_STEPS[names.get(rule, rule)]
        assert steps and not any("0x" in s or "eth_call" in s or "ABI" in s for s in steps)  # no jargon or hex


def _failures():
    for path in sorted(GOLDEN.glob("*.json")):
        data = json.loads(path.read_text())
        if any(e["kind"] == "diagnosis" for e in data["items"]):
            yield path.stem, data


@pytest.mark.parametrize("fixture, data", list(_failures()))
def test_every_recorded_failure_has_a_labelled_conclusion_with_both_steps(fixture, data):
    for e in (e for e in data["items"] if e["kind"] == "diagnosis"):
        assert e["data"]["label"] in ("CONFIRMED", "LIKELY", "UNKNOWN")
        assert e["text"].startswith(e["data"]["label"] + ": ")
        assert e["data"]["next_steps"]["support"] and e["data"]["next_steps"]["developer"]
        assert "Next step for a non-technical reader:" in e["text"] and "Next step for a developer:" in e["text"]


def test_the_real_celo_balance_failure_is_confirmed_and_its_steps_differ_by_reader():
    _c, tx = _case("celo_fail_balance_confirmed")
    b = replay_bundle(load_config(str(ROOT / "configs" / "celo-mainnet.yaml")), tx, "celo_fail_balance_confirmed")
    [d] = [e for e in b.items if e.kind == "diagnosis"]
    assert d.data["label"] == "CONFIRMED" and d.text.startswith("CONFIRMED: Cause confirmed")
    assert d.data["next_steps"]["support"] != d.data["next_steps"]["developer"]


def test_the_label_is_in_the_event_log(event_log, monkeypatch):
    from typer.testing import CliRunner

    from anychain import cli
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml"), "--no-llm"])
    [row] = event_log.summary(0)
    assert (row["confirmed"], row["likely"], row["unknown"]) == (1, 0, 0)


# ---- review of T5 ----

from anychain.collectors.types import RevertReason  # noqa: E402
from anychain.diagnosis import Context, diagnose  # noqa: E402
from anychain.reads import Read  # noqa: E402
from tests.test_rules_t4 import OTHER, SENDER, TARGET, FakeReader, _custom, _text  # noqa: E402


def _ctx(**kw):
    base = dict(reason=None, result=None, call=None, sender=SENDER, to=TARGET, to_text="the contract", block=100,
                gas_used=1, gas_limit=100, reader=None)
    return Context(**{**base, **kw})


def _label(finding):
    return label_for(finding.rule, finding.level, finding.label)


class PausedReader:
    def paused(self, contract, block):
        return Read("paused()", contract, (), block, False)


@pytest.mark.parametrize("ctx", [
    _ctx(reason=_text("Pausable: paused"), reader=PausedReader()),  # the read shows it was not paused
    _ctx(reason=_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]),
         reader=FakeReader(owner=SENDER)),  # the sender held the permission
    _ctx(reason=_text("UniswapV2Router: EXPIRED"), call={"function": "swap", "args": {"deadline": 2_000_000_000}},
         block_time=1_791_479_507),  # this call's deadline had not passed
    _ctx(explorer_text="Some explorer text"),  # the explorer's own words, not a decoded reason
])
def test_a_conclusion_its_own_data_contradicts_or_does_not_decode_is_likely(ctx):
    assert _label(diagnose(ctx)) == "LIKELY"


def test_the_bare_owner_text_does_not_say_the_sender_was_the_caller():
    f = diagnose(_ctx(reason=_text("Ownable: caller is not the owner"), reader=FakeReader(owner=OTHER)))
    assert "its caller is not" not in f.text and "the sender or a contract in between" in f.text


@pytest.mark.parametrize("rule", ["deadline", "access_control", "paused", "slippage", "possibly_out_of_gas"])
def test_support_steps_assert_no_cause_the_facts_do_not_prove(rule):
    text = " ".join(SUPPORT_STEPS[rule]).lower()
    for claim in ("took too long", "sets a new time limit", "reserved for", "is paused for now",
                  "the price changed", "try again from the app"):
        assert claim not in text, (rule, claim)


def test_a_failure_answered_from_the_node_only_is_logged_with_a_label():
    from anychain.events import RunEvent
    _c, tx = _case("celo_fail_balance_rpc_only")
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    b = replay_bundle(cfg, tx, "celo_fail_balance_rpc_only", offline_hosts={"celo.blockscout.com"})
    assert b.status == "failed" and RunEvent.from_bundle(b, "cli", 1).diagnosis == "LIKELY"  # the replay's


def test_an_old_log_without_the_label_column_is_migrated(tmp_path):
    import sqlite3

    from anychain.events import RunEvent, SqliteEventLog
    path = tmp_path / "old_log.db"  # runs.db is the suite's own per-test log
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL NOT NULL, network TEXT NOT NULL, tx_hash TEXT "
                     "NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, status TEXT, "
                     "facts INTEGER NOT NULL, error TEXT, writer TEXT);")
    db.close()
    log = SqliteEventLog(path)
    log.record(RunEvent(network="n", tx_hash="0x1", source="cli", outcome="ok", duration_ms=1, status="failed",
                        diagnosis="UNKNOWN"))
    assert log.summary(0)[0]["unknown"] == 1


def test_each_mode_is_told_which_steps_to_give():
    from anychain.writer import load_prompt
    for mode in ("support", "developer", "auditor"):
        prompt = load_prompt(mode, "en")
        assert f"Mode: {mode}." in prompt
        assert 'In support mode give only the steps "for a non-technical reader"' in prompt
        assert 'in developer and auditor modes give the steps "for a developer"' in prompt


def test_the_log_command_prints_the_label_counts(event_log, monkeypatch):
    from typer.testing import CliRunner

    from anychain import cli
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml"), "--no-llm"])
    monkeypatch.setattr(cli, "SqliteEventLog", lambda _path, read_only=False: event_log)  # not the real log
    out = CliRunner().invoke(cli.app, ["log", "--config", str(ROOT / "configs" / "celo-mainnet.yaml")]).output
    assert "CONFIRMED 1" in out and "LIKELY 0" in out and "UNKNOWN 0" in out
