"""The CLI: clear messages and exit codes, never a stack trace."""
from typer.testing import CliRunner

from anychain import cli
from tests.conftest import ROOT, USDC_TX, replay_bundle

runner = CliRunner()
ETH = str(ROOT / "configs" / "ethereum-mainnet.yaml")


def test_invalid_hash():
    result = runner.invoke(cli.app, ["explain", "0x123", "--config", ETH])
    assert result.exit_code == 1 and "valid transaction hash" in result.output
    assert "Traceback" not in result.output


def test_missing_config():
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", "nope.yaml"])
    assert result.exit_code == 1 and "not found" in result.output


def test_unknown_mode_is_rejected_up_front():
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH, "--mode", "foo"])
    assert result.exit_code == 2 and "foo" in result.output


def test_unexpected_error_is_a_message_not_a_traceback(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("something odd")
    monkeypatch.setattr(cli, "build_bundle", boom)
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH])
    assert result.exit_code == 1 and "Unexpected error" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_evidence_output_offline(monkeypatch, eth_cfg):
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "eth_usdc_transfer"))
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH, "--no-llm"])
    assert result.exit_code == 0 and "**[E1]**" in result.output and "69.3484 USDC" in result.output


def test_each_answer_is_logged_with_its_gap_causes(monkeypatch, event_log):
    # eth_usdc_rpc_only was recorded with the explorer down: the gaps are source outages.
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(
        cfg, h, "eth_usdc_rpc_only", offline_hosts={"eth.blockscout.com"}))
    from tests.test_golden import _case
    _config, tx = _case("eth_usdc_rpc_only")
    assert runner.invoke(cli.app, ["explain", tx, "--config", ETH, "--no-llm"]).exit_code == 0
    [row] = event_log.summary(0)
    assert (row["network"], row["answers"], row["degraded"], row["crashes"]) == ("ethereum-mainnet", 1, 1, 0)
    assert {(g["cause"], g["topic"]) for g in row["gaps"]} >= {("source_unavailable", "Explorer transaction data")}


def test_a_crash_is_logged(monkeypatch, event_log):
    def boom(*_a, **_k):
        raise RuntimeError("something odd")
    monkeypatch.setattr(cli, "build_bundle", boom)
    runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH])
    [problem] = event_log.problems(0)
    assert problem["outcome"] == "crash" and "something odd" in problem["error"]


def test_logging_failure_never_costs_the_answer(monkeypatch, eth_cfg):
    class Broken:
        def record(self, _event):
            raise OSError("disk full")
    monkeypatch.setattr(cli, "event_log_for", lambda _s, _p: Broken())
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "eth_usdc_transfer"))
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH, "--no-llm"])
    assert result.exit_code == 0 and "69.3484 USDC" in result.output


def test_log_command_reports_problem_rates(monkeypatch, event_log):
    from anychain.events import GapEvent, RunEvent
    event_log.record(RunEvent("celo-mainnet", "0x" + "1" * 64, "cli", "degraded", 900, "success", 5,
                              gaps=(GapEvent("Fee", "processing_error", "could not process the data", False),)))
    event_log.record(RunEvent("celo-mainnet", "0x" + "2" * 64, "cli", "ok", 700, "success", 6,
                              gaps=(GapEvent("Call decoding", "not_interpretable", "no ABI", False),)))
    monkeypatch.setattr(cli, "SqliteEventLog", lambda _p, **_kw: event_log)
    out = runner.invoke(cli.app, ["log", "--config", ETH]).output
    assert "celo-mainnet: 2 answers, 1 with a problem, 0 crashed" in out
    assert "processing_error" in out and "50.0%" in out
    assert "not_interpretable" not in out  # expected limits are not problems
    out = runner.invoke(cli.app, ["log", "--config", ETH, "--problems"]).output
    assert "0x" + "1" * 64 in out and "0x" + "2" * 64 not in out


def test_a_log_that_cannot_open_never_costs_the_answer(monkeypatch, eth_cfg, tmp_path):
    # Real construction, no patched logger: the path's parent is a file, so mkdir fails.
    from anychain.events import event_log_for
    monkeypatch.setattr(cli, "event_log_for", event_log_for)
    (tmp_path / "blocker").write_text("")
    cfg = eth_cfg.model_copy(deep=True)
    cfg.storage.sqlite_path = str(tmp_path / "blocker" / "runs.db")
    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "build_bundle", lambda h, c: replay_bundle(c, h, "eth_usdc_transfer"))
    result = runner.invoke(cli.app, ["explain", USDC_TX, "--config", ETH, "--no-llm"])
    assert result.exit_code == 0 and "69.3484 USDC" in result.output


def test_a_recheck_replaces_the_earlier_answer(event_log):
    from anychain.events import CheckEvent, RunEvent
    tx = "0x" + "3" * 64
    event_log.record(RunEvent("zksync-era", tx, "canary", "ok", 900, "success", 5))
    event_log.record(RunEvent("zksync-era", tx, "canary", "degraded", 900, "success", 5,
                              checks=(CheckEvent("l1_status", "fail", "denied"),)), replace=True)
    [row] = event_log.summary(0)
    assert (row["answers"], row["degraded"]) == (1, 1)
    assert event_log.has("zksync-era", "canary", tx) and not event_log.has("celo-mainnet", "canary", tx)


def test_log_query_never_creates_a_log(tmp_path):
    from anychain.events import SqliteEventLog
    missing = tmp_path / "nowhere" / "runs.db"
    try:
        SqliteEventLog(missing, read_only=True)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    assert not missing.parent.exists()


def test_read_only_log_survives_odd_characters_in_the_path(tmp_path):
    from anychain.events import SqliteEventLog
    path = tmp_path / "a b?c#d" / "runs.db"
    SqliteEventLog(path)  # create it normally first
    before = sorted(p.name for p in tmp_path.rglob("*"))
    SqliteEventLog(path, read_only=True).summary(0)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
