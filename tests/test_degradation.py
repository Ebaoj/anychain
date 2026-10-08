"""PHASE2 T8 (R8): the degradation matrix. Every source the tool uses, missing one at a time (and the two
main ones together), on real recorded transactions. In every case the tool must:
  1. answer without crashing;
  2. say what is missing, with a cause and what would fix it;
  3. state nothing that needs the missing source;
  4. log it (the event log counts the gap causes).
"""
import httpx
import pytest
from typer.testing import CliRunner

from anychain import cli
from anychain.bundle import BundleBuilder
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import Budget
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from anychain.events import PROBLEM_CAUSES
from tests.conftest import ROOT, USDC_TX, make_transport, replay_bundle

ETH = str(ROOT / "configs" / "ethereum-mainnet.yaml")
EXPLORER, NODE = "eth.blockscout.com", "ethereum-rpc.publicnode.com"


def _eth():
    return load_config(ETH)


def _causes(b, what=None):
    return {g.cause for g in b.gaps if what is None or g.what == what}


def _well_formed(b):
    """Contract shared by every case: each gap explains itself, each fact has a source."""
    for g in b.gaps:
        assert g.why and g.needed and g.cause, g
    for e in b.items:
        assert e.sources, e.id


RPC_ONLY_TX = "0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952"  # recorded with the explorer down


def test_explorer_down():
    b = replay_bundle(_eth(), RPC_ONLY_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER})
    _well_formed(b)
    assert b.status == "success"  # from the node's receipt
    assert {s.kind for e in b.items for s in e.sources} <= {"rpc", "signature_db"}  # nothing from the explorer
    assert _causes(b, "Explorer transaction data") == {"source_unavailable"}
    assert _causes(b, "Event decoding") == {"source_unavailable"}


def test_node_down():
    b = replay_bundle(_eth(), USDC_TX, "eth_usdc_transfer", offline_hosts={NODE})
    _well_formed(b)
    assert b.status == "success" and b.items  # from the explorer
    assert all(s.kind != "rpc" for e in b.items for s in e.sources)  # nothing rests on the node
    assert all(e.confidence != "confirmed" for e in b.items)  # so nothing is "confirmed by the node"
    assert _causes(b) == {"source_unavailable"} and _causes(b, "RPC transaction data") == {"source_unavailable"}


def test_explorer_and_node_down():
    b = replay_bundle(_eth(), USDC_TX, "eth_usdc_transfer", offline_hosts={EXPLORER, NODE})
    _well_formed(b)
    assert b.items == [] and b.status == "unknown"  # nothing to state, and nothing is
    assert {g.what: g.cause for g in b.gaps} == {"Explorer transaction data": "source_unavailable",
                                                 "RPC transaction data": "source_unavailable"}


def test_explorer_slow_on_one_list():
    b = replay_bundle(_eth(), USDC_TX, "eth_usdc_transfer", overrides={"/logs": {"timeout": True}})
    _well_formed(b)
    assert b.status == "success" and _causes(b, "Events") == {"source_unavailable"}
    assert all(g.retryable for g in b.gaps if g.what == "Events")


def test_time_budget_used_up(monkeypatch):
    # The whole explanation's budget is gone before the first request: every source reports it, nothing
    # is stated, nothing crashes.
    cfg = _eth()
    transport = make_transport("eth_usdc_transfer")
    client = httpx.Client(transport=transport)
    spent = Budget(0)
    b = BundleBuilder(cfg, ExplorerClient(cfg.explorer, client, spent), RpcClient(cfg.rpc, client, spent)).build(USDC_TX)
    _well_formed(b)
    assert b.items == [] and b.status == "unknown" and any("time budget" in g.why for g in b.gaps)
    assert _causes(b) == {"source_unavailable"} and all(g.retryable for g in b.gaps)
    assert transport.calls == []  # no request was made after the budget ran out


def test_node_without_old_state_for_the_diagnosis():
    from tests.test_golden import _case
    _config, tx = _case("celo_fail_balance_confirmed")
    refusal = {"status": 403, "body": '{"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a '
                                      'personal token."},"id":1}'}
    b = replay_bundle(load_config(str(ROOT / "configs" / "celo-mainnet.yaml")), tx, "celo_fail_balance_confirmed",
                      overrides={f'"{hex(78962883)}"]': refusal})
    _well_formed(b)
    [finding] = [e for e in b.items if e.kind == "diagnosis"]
    assert finding.confidence != "confirmed" and "source_error" in _causes(b, "Diagnosis")


def test_repo_not_synced(monkeypatch, tmp_path):
    from anychain.collectors import repo as repo_module
    monkeypatch.setattr(repo_module.RepoCache, "__init__", lambda self, _d: setattr(self, "root", tmp_path))
    b = replay_bundle(_eth(), "0x610935d36b23bd879133c5344f805bc144b1c254ab78cefae8db79dcb24659ef", "eth_brlc_set_pauser")
    _well_formed(b)
    assert not [e for e in b.items if e.kind == "source"] and _causes(b, "Source code") == {"config_error"}


def _signature_db_online(monkeypatch):
    from anychain.collectors.signatures import SignatureDb
    suite_init = SignatureDb.__init__

    def online(self, *a, **k):
        suite_init(self, *a, **k)
        self.offline = False  # requests go through the recorded transport, where the override answers
    monkeypatch.setattr(SignatureDb, "__init__", online)
    monkeypatch.setattr(SignatureDb, "_read", lambda self: {})


@pytest.mark.parametrize("answer, cause", [
    ({"timeout": True}, "source_unavailable"),
    ({"status": 503, "body": "busy"}, "source_unavailable"),
    ({"status": 200, "body": "<html>maintenance</html>"}, "source_unavailable"),
    ({"status": 200, "body": '{"unexpected": true}'}, "source_error"),
])
def test_signature_database_failing_in_each_way(monkeypatch, answer, cause):
    _signature_db_online(monkeypatch)
    b = replay_bundle(_eth(), RPC_ONLY_TX, "eth_usdc_rpc_only", offline_hosts={EXPLORER},
                      overrides={"&ordering=created_at": answer})  # the real request path, a broken answer
    _well_formed(b)
    assert not [e for e in b.items if e.kind == "candidate"]
    assert _causes(b, "Signature database") == {cause}


def test_failed_transaction_with_the_explorer_down_says_the_reason_is_missing_and_replays():
    # Real Celo failure recorded with the explorer down (review of T8: the reason's absence was not said).
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    tx = "0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817"
    b = replay_bundle(cfg, tx, "celo_fail_balance_rpc_only", offline_hosts={"celo.blockscout.com"})
    _well_formed(b)
    assert b.status == "failed"  # the node's receipt status
    assert _causes(b, "Revert reason") == {"source_unavailable"}  # what is missing, and why
    assert not [e for e in b.items if e.kind == "revert"]  # no reason is stated as the explorer's
    [replay] = [e for e in b.items if e.kind == "replay"]
    assert "transfer amount exceeds balance" in replay.text and all(s.kind == "rpc" for s in replay.sources)
    assert all(e.confidence == "candidate" for e in b.items if e.kind == "diagnosis")


def test_llm_unavailable_gives_the_evidence_and_logs_it(monkeypatch, event_log):
    from anychain import writer

    class Down:
        def complete(self, system, user):
            raise writer.WriterError("Claude Code not found")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "eth_usdc_transfer"))
    monkeypatch.setattr(writer, "backend_for", lambda llm: Down())
    result = CliRunner().invoke(cli.app, ["explain", USDC_TX, "--config", ETH])
    assert result.exit_code == 0 and "**[E1]**" in result.output and "LLM unavailable" in result.output
    [row] = event_log.summary(0)
    assert row["unavailable"] == 1 and row["degraded"] == 1


@pytest.mark.parametrize("offline", [{EXPLORER}, {NODE}, {EXPLORER, NODE}])
def test_outages_are_logged_as_problems(monkeypatch, event_log, offline):
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "eth_usdc_transfer",
                                                                          offline_hosts=offline))
    CliRunner().invoke(cli.app, ["explain", USDC_TX, "--config", ETH, "--no-llm"])
    [row] = event_log.summary(0)
    assert row["degraded"] == 1
    assert any(g["cause"] in PROBLEM_CAUSES for g in row["gaps"])


def test_a_slow_explorer_does_not_take_the_nodes_time(monkeypatch):
    # Acceptance run, 2026-10-08: the explorer used the whole 30 s budget on the transaction (three 10 s
    # timeouts) and the node was never asked, so the answer was "unknown" while the node knew it succeeded.
    import threading

    from anychain.collectors.http import CollectorError
    real = ExplorerClient.transaction

    def slow(self, tx_hash):
        threading.Event().wait(0.3)  # longer than the whole budget below (not time.sleep: the suite stubs it)
        raise CollectorError(f"time budget of 0s used up before {tx_hash} (ReadTimeout)", retryable=True)
    monkeypatch.setattr(ExplorerClient, "transaction", slow)
    cfg = _eth()
    client = httpx.Client(transport=make_transport("eth_usdc_transfer"))
    budget = Budget(0.2)
    b = BundleBuilder(cfg, ExplorerClient(cfg.explorer, client, budget), RpcClient(cfg.rpc, client, budget)).build(USDC_TX)
    assert b.status == "success"  # the node answered in time
    monkeypatch.setattr(ExplorerClient, "transaction", real)


def test_a_mined_transaction_without_a_receipt_is_never_called_pending():
    # Real (acceptance 2026-10-08): ethereum-rpc.publicnode.com returns tx 0xe40d5210… with its block (25947205)
    # but a null receipt; with the explorer down the tool said "Transaction is pending" (a level A false fact).
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    tx = "0xe40d5210e5186af42683ef029caa7e56f6f6527975ca64be268305171c9ac957"
    b = replay_bundle(cfg, tx, "eth_receipt_missing", offline_hosts={"eth.blockscout.com"})
    assert b.status == "unknown"
    overview = next(e for e in b.items if e.kind == "overview")
    assert "pending" not in overview.text.lower() and "25947205" in overview.text
    assert not any(g.cause == "pending" for g in b.gaps)
    assert any(g.what == "Final outcome" and g.cause == "source_error" and g.retryable
               and "explorer did not answer" in g.why for g in b.gaps)
    from anychain.answer import structured_answer
    confidence = structured_answer(b, None, "skipped", "support")["confidence"]
    assert (confidence["status"], confidence["status_basis"]) == (None, "outcome_unknown")  # never "confirmed"
