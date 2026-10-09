"""PHASE3 T1 (R6, D45): what each answer records, and `anychain metrics` with its SQL."""
import json
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from anychain import cli
from anychain.config import load_config
from anychain.events import RunEvent, SqliteEventLog, abi_source_of
from anychain.writer import ClaudeCodeBackend
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

CLAUDE_OUTPUT = (ROOT / "tests" / "fixtures_llm" / "claude_code_output.json").read_text()  # a real `claude -p` answer


def test_claude_code_reports_its_tokens_and_cost(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _c: "/usr/bin/claude")
    llm = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml")).llm
    backend = ClaudeCodeBackend(llm, run=lambda *a, **k: subprocess.CompletedProcess(a, 0, CLAUDE_OUTPUT, ""))
    assert backend.complete("system", "user") == "ok"
    real = json.loads(CLAUDE_OUTPUT)
    assert backend.last_usage == {"input_tokens": real["usage"]["input_tokens"],
                                  "output_tokens": real["usage"]["output_tokens"],
                                  "cache_read_tokens": real["usage"]["cache_read_input_tokens"],
                                  "cache_write_tokens": real["usage"]["cache_creation_input_tokens"],
                                  "cost_usd": real["total_cost_usd"]}


def test_a_retried_answer_adds_both_attempts(monkeypatch):
    from anychain.writer import write_checked
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    _c, tx = _case("eth_usdc_transfer")
    b = replay_bundle(cfg, tx, "eth_usdc_transfer")

    class Backend:
        answers = iter(["It moved 999999 USDC [E1].", "It moved [E1]."])  # the first is caught by the check
        last_usage = None

        def complete(self, system, user):
            self.last_usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
                               "cache_write_tokens": 0, "cost_usd": 0.5}
            return next(self.answers)
    checked = write_checked(b, cfg, "support", backend=Backend())
    assert checked.outcome == "retried" and checked.usage["input_tokens"] == 20 and checked.usage["cost_usd"] == 1.0


def _cfg(name):
    return load_config(str(ROOT / "configs" / f"{name}.yaml"))


def test_the_abi_source_of_the_main_call():
    def source(config, fixture, **kw):
        _c, tx = _case(fixture)
        return abi_source_of(replay_bundle(_cfg(config), tx, fixture, **kw))
    assert source("ethereum-mainnet", "eth_usdc_transfer") == "explorer"
    assert source("ethereum-mainnet", "eth_failed_unverified_bot") == "raw"  # no candidate for its selector
    assert source("ethereum-mainnet", "eth_usdc_rpc_only", offline_hosts={"eth.blockscout.com"}) == "signature_db"
    assert source("rootstock-mainnet", "rootstock_transfer") == "explorer"  # RIF token transfer
    assert source("zksync-era", "zksync_native_eth_send") is None  # a plain ETH send: no contract call
    from tests.test_artifacts import _unverified
    assert source("ethereum-mainnet", "eth_safe_deploy", overrides=_unverified()) == "repo_artifacts"
    # a pinned contract decoded from repo source, and an unpinned selector match (a candidate): not the same bucket
    from tests.test_repos import SET_PAUSER
    from tests.test_repos import _pinned as pin_brlc
    from tests.test_repos import _unverified as brlc_unverified
    eth = _cfg("ethereum-mainnet")
    assert abi_source_of(replay_bundle(pin_brlc(eth), SET_PAUSER, "eth_brlc_set_pauser",
                                       overrides=brlc_unverified())) == "repo_source"
    assert abi_source_of(replay_bundle(eth, SET_PAUSER, "eth_brlc_set_pauser",
                                       overrides=brlc_unverified())) == "repo_candidate"


def test_an_answer_records_mode_rule_abi_source_tokens_and_cost(monkeypatch, event_log):
    from anychain.writer import CheckedAnswer
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    usage = {"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 50, "cache_write_tokens": 0,
             "cost_usd": 0.02}
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: CheckedAnswer("It failed [E1].", "ok", [], usage))
    CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml"),
                                 "--mode", "developer"])
    [row] = event_log.recent(1)
    assert (row["mode"], row["rule"], row["abi_source"]) == ("developer", "insufficient_balance", "explorer")
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (1200, 300, 0.02)


def test_feedback_is_saved_on_its_answer(event_log):
    run_id = event_log.record(RunEvent(network="n", tx_hash="0x1", source="api", outcome="ok", duration_ms=1,
                                       mode="support"))
    event_log.set_feedback(run_id, "up")
    assert event_log.recent(1)[0]["feedback"] == "up"


def _seed(log: SqliteEventLog):
    rows = [("support", "failed", "insufficient_balance", "CONFIRMED", "explorer", "up", "stored"),
            ("support", "failed", "no_reason", "UNKNOWN", "raw", "down", "not kept: x"),
            ("developer", "failed", "insufficient_balance", "CONFIRMED", "explorer", "up", "hit"),
            ("developer", "success", None, None, "repo_artifacts", None, "hit"),
            ("auditor", "success", None, None, None, None, "off")]
    for i, (mode, status, rule, label, abi, feedback, cache) in enumerate(rows):
        run_id = log.record(RunEvent(network="ethereum-mainnet", tx_hash=f"0x{i}", source="cli", outcome="ok",
                                     duration_ms=10, status=status, mode=mode, rule=rule, diagnosis=label,
                                     abi_source=abi, cache=cache))
        if feedback:
            log.set_feedback(run_id, feedback)


def test_metrics_count_users_answers_only(event_log):
    from anychain.metrics import run_queries
    _seed(event_log)
    # the acceptance run's rows, and rows written before the metrics columns (no mode), are not users' answers
    event_log.record(RunEvent(network="ethereum-mainnet", tx_hash="0x2", source="canary", outcome="ok",
                              duration_ms=1, status="failed", mode=None, rule="no_reason", diagnosis="UNKNOWN"))
    event_log.record(RunEvent(network="ethereum-mainnet", tx_hash="0x3", source="cli", outcome="ok",
                              duration_ms=1, status="failed", diagnosis="CONFIRMED"))  # before T1: no mode
    causes, labels, abi, satisfaction, cache, _chat = run_queries(event_log, 0)
    assert {r["cause"]: r["answers"] for r in causes} == {"insufficient_balance": 2, "no_reason": 1}
    assert {r["label"]: r["answers"] for r in labels} == {"CONFIRMED": 2, "UNKNOWN": 1}
    assert {r["mode"]: (r["up"], r["down"], r["satisfaction"]) for r in satisfaction} == {
        "auditor": (0, 0, None), "developer": (1, 0, 100.0), "support": (1, 1, 50.0)}
    assert {r["cache"]: r["answers"] for r in cache} == {"hit": 2, "stored": 1, "not kept": 1, "off": 1}
    assert {r["abi_source"]: r["answers"] for r in abi} == {"explorer": 2, "raw": 1, "repo_artifacts": 1}


def test_metrics_print_each_query_with_its_sql(monkeypatch, event_log):
    _seed(event_log)
    monkeypatch.setattr(cli, "SqliteEventLog", lambda _path, read_only=False: event_log)
    out = CliRunner().invoke(cli.app, ["metrics", "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml")]).output
    for title in ("Failure causes", "Diagnosis labels", "ABI source of the main call", "Satisfaction by mode",
                  "Cache"):
        assert title in out
    assert out.count("SELECT") == 6  # every number comes with the query that made it
    assert "insufficient_balance | 2 | 2 | 66.7%" in out and "support | 1 | 1 | 50.0%" in out
    assert "all networks in this log" in out


def test_metrics_on_a_log_from_before_these_columns(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)  # the first version's table, read-only: never migrated by a query
    db.executescript("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL NOT NULL, network TEXT NOT NULL, tx_hash TEXT "
                     "NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, status "
                     "TEXT, facts INTEGER NOT NULL, error TEXT); INSERT INTO runs VALUES (1, 9e9, 'n', '0x1', 'cli', "
                     "'ok', 1, 'failed', 3, NULL);")
    db.commit()
    db.close()
    old = SqliteEventLog(path, read_only=True)
    monkeypatch.setattr(cli, "SqliteEventLog", lambda _path, read_only=False: old)
    result = CliRunner().invoke(cli.app, ["metrics", "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml")])
    assert result.exit_code == 0 and result.output.count("column yet") == 6  # none of the six queries' columns exists
    assert "(no answers in this period)" not in result.output


def test_tokens_keep_the_cache_reads_and_a_failed_retry_keeps_what_was_spent(monkeypatch):
    from anychain.writer import WriterError, write_checked
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    _c, tx = _case("eth_usdc_transfer")
    b = replay_bundle(cfg, tx, "eth_usdc_transfer")

    class Backend:
        calls = 0
        last_usage = None

        def complete(self, system, user):
            self.calls += 1
            if self.calls == 2:
                raise WriterError("the model stopped answering")
            self.last_usage = {"input_tokens": 2, "output_tokens": 5, "cache_read_tokens": 9000,
                               "cache_write_tokens": 100, "cost_usd": 0.1}
            return "It moved 999999 USDC [E1]."  # caught by the check: a retry is asked
    try:
        write_checked(b, cfg, "support", backend=Backend())
    except WriterError as exc:
        assert exc.usage["cache_read_tokens"] == 9000 and exc.usage["input_tokens"] == 2
    else:
        raise AssertionError("the failed retry must raise")


def test_the_log_records_cache_tokens(monkeypatch, event_log):
    from anychain.writer import CheckedAnswer
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    usage = {"input_tokens": 2, "output_tokens": 300, "cache_read_tokens": 9000, "cache_write_tokens": 100,
             "cost_usd": 0.02}
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: CheckedAnswer("It failed [E1].", "ok", [], usage))
    CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml")])
    row = event_log.recent(1)[0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (9000, 100)
