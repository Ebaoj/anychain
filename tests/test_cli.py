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
