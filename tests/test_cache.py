"""PHASE3 T0 (R10, R11, D44): answers kept by (network, hash) when their facts cannot change, and the batch command.

Finality comes from real node answers recorded with scripts/record_finality.py on 2026-10-08.
"""
import json

import httpx
import pytest
from typer.testing import CliRunner

from anychain import cache as cache_module
from anychain import cli
from anychain.cache import BundleCache, keep_decision
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from tests.conftest import ROOT, make_transport, mutated, replay_bundle
from tests.test_golden import _case


def _cfg(name):
    return load_config(str(ROOT / "configs" / f"{name}.yaml"))


def _rpc(cfg, fixture, overrides=None):
    return RpcClient(cfg.rpc, httpx.Client(transport=make_transport(fixture, overrides=overrides)))


def _decide(config, fixture, overrides=None, offline=None):
    cfg = _cfg(config)
    _c, tx = _case(fixture)
    b = replay_bundle(cfg, tx, fixture, offline_hosts=offline)
    return keep_decision(b, _rpc(cfg, fixture, overrides), cfg)


def test_a_final_transaction_with_no_open_gap_is_kept():
    assert _decide("ethereum-mainnet", "eth_usdc_transfer") is None  # block 26141501, finalized 26149453
    assert _decide("ethereum-mainnet", "eth_fail_expired_v2") is None  # a failure is final too


def test_zksync_before_its_l1_execution_is_not_kept():
    # block 72376921, after the node's finalized block 72376287 (= the last one executed on L1)
    reason = _decide("zksync-era", "zksync_native_eth_send")
    assert reason and "not final" in reason
    # final for the node (block 72076991), but the explorer's L1 status ("Sealed on L2") will still change
    reason = _decide("zksync-era", "zksync_explorer_l1_behind")
    assert reason and "L1 status" in reason
    # final too (block 60000000), but its call is not decoded: verification later could fill it
    assert _decide("zksync-era", "zksync_executed_on_l1") == "gap that can be filled later: Call decoding"


def test_a_node_without_the_finalized_tag_uses_confirmations():
    assert _decide("rootstock-mainnet", "rootstock_transfer") is None  # Rootstock rejects "finalized"
    cfg = _cfg("rootstock-mainnet").model_copy(deep=True)
    cfg.cache.min_confirmations = 10**9
    _c, tx = _case("rootstock_transfer")
    b = replay_bundle(cfg, tx, "rootstock_transfer")
    reason = keep_decision(b, _rpc(cfg, "rootstock_transfer"), cfg)
    assert reason and "confirmations" in reason


def test_a_block_after_the_finalized_one_is_not_kept():
    # the real recorded answer, with its number moved before the transaction's block
    over = mutated("eth_usdc_transfer", 'eth_getBlockByNumber ["finalized", false]',
                   lambda body: body["result"].update(number=hex(26141500)))
    reason = _decide("ethereum-mainnet", "eth_usdc_transfer", overrides=over)
    assert reason and "not final" in reason


def test_open_gaps_are_never_kept():
    reason = _decide("ethereum-mainnet", "eth_usdc_transfer", offline={"eth.blockscout.com"})
    assert reason and "gap" in reason


def test_a_failed_finality_check_is_not_kept():
    cfg = _cfg("ethereum-mainnet")
    _c, tx = _case("eth_usdc_transfer")
    b = replay_bundle(cfg, tx, "eth_usdc_transfer")
    offline = RpcClient(cfg.rpc, httpx.Client(transport=make_transport("eth_usdc_transfer",
                                                                       {"ethereum-rpc.publicnode.com"})))
    assert "finality" in keep_decision(b, offline, cfg)


def test_the_key_changes_with_the_code_and_the_config(tmp_path, monkeypatch):
    cfg = _cfg("ethereum-mainnet")
    _c, tx = _case("eth_usdc_transfer")
    store = BundleCache(tmp_path / "c.db")
    store.put_bundle(cfg, tx, replay_bundle(cfg, tx, "eth_usdc_transfer"))
    assert store.get_bundle(cfg, tx) is not None and store.get_bundle(cfg, tx.upper().replace("0X", "0x")) is not None
    other = cfg.model_copy(deep=True)
    other.address_labels["0x" + "11" * 20] = "Someone"  # a config change that can change facts
    assert store.get_bundle(other, tx) is None
    monkeypatch.setattr(cache_module, "CODE_DIGEST", "another version of the code")
    assert store.get_bundle(cfg, tx) is None


def test_old_entries_expire(tmp_path):
    cfg = _cfg("ethereum-mainnet")
    _c, tx = _case("eth_usdc_transfer")
    store = BundleCache(tmp_path / "c.db")
    store.put_bundle(cfg, tx, replay_bundle(cfg, tx, "eth_usdc_transfer"), now=0)
    assert store.get_bundle(cfg, tx) is None  # written in 1970


def _patch_cli(monkeypatch, fixture, transport_box):
    def build(h, cfg):
        transport_box["t"] = make_transport(fixture)
        client = httpx.Client(transport=transport_box["t"])
        from anychain.bundle import build_bundle
        from anychain.collectors.explorer import ExplorerClient
        return build_bundle(h, cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
    monkeypatch.setattr(cli, "build_bundle", build)
    monkeypatch.setattr(cli, "finality_rpc_for", lambda cfg: _rpc(cfg, fixture))


def test_the_second_explain_is_served_from_the_cache(monkeypatch, tmp_path, event_log):
    monkeypatch.setattr(cli, "cache_for", lambda cfg: BundleCache(tmp_path / "c.db"))
    box = {}
    _patch_cli(monkeypatch, "eth_usdc_transfer", box)
    _c, tx = _case("eth_usdc_transfer")
    args = ["explain", tx, "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"), "--evidence"]
    first = json.loads(CliRunner().invoke(cli.app, args).stdout)
    box.clear()
    second = json.loads(CliRunner().invoke(cli.app, args).stdout)
    assert "t" not in box  # nothing was fetched
    assert second == first
    assert [r["cache"] for r in event_log.recent(10)] == ["hit", "stored"]  # newest first
    fresh = CliRunner().invoke(cli.app, args + ["--fresh"])
    assert fresh.exit_code == 0 and "t" in box


def test_batch_resumes_where_it_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "cache_for", lambda cfg: BundleCache(tmp_path / "c.db"))
    fixtures = {_case(f)[1]: f for f in ("eth_usdc_transfer", "eth_fail_expired_v2")}

    def build(h, cfg):
        return replay_bundle(cfg, h, fixtures[h])
    monkeypatch.setattr(cli, "build_bundle", build)
    monkeypatch.setattr(cli, "finality_rpc_for", lambda cfg: None)
    hashes = tmp_path / "hashes.txt"
    hashes.write_text("\n".join(list(fixtures) + ["# a comment", "", list(fixtures)[0]]))
    out = tmp_path / "out.jsonl"
    out.write_text(json.dumps({"tx_hash": list(fixtures)[0], "answer": {}}) + "\n")  # done in an earlier run
    result = CliRunner().invoke(cli.app, ["batch", str(hashes), "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"),
                                          "--out", str(out)])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["tx_hash"] for line in lines] == list(fixtures)  # each once, the first kept from before
    assert lines[1]["answer"]["schema"] == "anychain.answer/1" and lines[1]["answer"]["status"] == "failed"


# ---- review of T0 ----

def test_only_an_unsupported_tag_falls_back_to_confirmations():
    cfg = _cfg("zksync-era")
    refused = {'eth_getBlockByNumber ["finalized", false]': {"status": 403, "body": "forbidden"}}
    rpc = _rpc(cfg, "zksync_native_eth_send", overrides=refused)
    with pytest.raises(Exception):
        rpc.finalized_block()  # a refusal is not "the node has no finalized tag"
    _c, tx = _case("zksync_native_eth_send")
    b = replay_bundle(cfg, tx, "zksync_native_eth_send")
    assert "finality not checked" in keep_decision(b, rpc, cfg)
    assert _rpc(_cfg("rootstock-mainnet"), "rootstock_transfer").finalized_block() is None  # Rootstock's real answer


class Broken:
    """A cache file that cannot be read or written (read-only disk, a lock held too long)."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise __import__("sqlite3").OperationalError("attempt to write a readonly database")
        return fail


def test_a_broken_cache_never_costs_the_answer(monkeypatch, event_log):
    from anychain.writer import CheckedAnswer
    monkeypatch.setattr(cli, "cache_for", lambda cfg: Broken())
    box = {}
    _patch_cli(monkeypatch, "eth_usdc_transfer", box)
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: CheckedAnswer("It moved [E1].", "ok", []))
    _c, tx = _case("eth_usdc_transfer")
    result = CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"),
                                          "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout)["summary"] == "It moved [E1]."
    [row] = event_log.recent(1)
    assert row["outcome"] != "crash" and row["cache"].startswith("not kept: cache error")


def test_a_written_answer_is_kept_and_said_to_come_from_the_cache(monkeypatch, tmp_path, event_log):
    from anychain.writer import CheckedAnswer
    monkeypatch.setattr(cli, "cache_for", lambda cfg: BundleCache(tmp_path / "c.db"))
    box = {}
    _patch_cli(monkeypatch, "eth_usdc_transfer", box)
    calls = []

    def write(b, cfg, mode):
        calls.append(mode)
        return CheckedAnswer(f"Written for {mode} [E1].", "ok", [])
    monkeypatch.setattr(cli, "write_checked", write)
    _c, tx = _case("eth_usdc_transfer")
    args = ["explain", tx, "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"), "--json"]
    for mode in ("support", "support", "developer"):
        out = json.loads(CliRunner().invoke(cli.app, args + ["--mode", mode]).stdout)
        assert out["summary"] == f"Written for {mode} [E1]."
    assert calls == ["support", "developer"]  # the second support answer came from the cache
    writers = [r["writer"] for r in event_log.recent(3)]
    assert writers == ["ok", "cached", "ok"]


def test_a_withheld_answer_is_never_kept(monkeypatch, tmp_path):
    from anychain.writer import CheckedAnswer
    monkeypatch.setattr(cli, "cache_for", lambda cfg: BundleCache(tmp_path / "c.db"))
    _patch_cli(monkeypatch, "eth_usdc_transfer", {})
    calls = []
    monkeypatch.setattr(cli, "write_checked", lambda b, cfg, mode: calls.append(1) or CheckedAnswer(None, "fail", ["x"]))
    _c, tx = _case("eth_usdc_transfer")
    args = ["explain", tx, "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"), "--json"]
    CliRunner().invoke(cli.app, args)
    CliRunner().invoke(cli.app, args)
    assert len(calls) == 2


def test_the_cache_can_be_turned_off(monkeypatch, event_log):
    from anychain.cache import NoCache
    monkeypatch.setattr(cli, "cache_for", lambda cfg: NoCache())
    _patch_cli(monkeypatch, "eth_usdc_transfer", {})
    _c, tx = _case("eth_usdc_transfer")
    CliRunner().invoke(cli.app, ["explain", tx, "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"), "--evidence"])
    assert event_log.recent(1)[0]["cache"] == "off"


def test_batch_survives_a_cut_last_line_and_hash_case(monkeypatch, tmp_path):
    fixtures = {_case(f)[1]: f for f in ("eth_usdc_transfer", "eth_fail_expired_v2")}
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, fixtures[h.lower()]))
    first, second = list(fixtures)
    hashes = tmp_path / "hashes.txt"
    hashes.write_text(f"{first}\n{first.upper().replace('0X', '0x')}\n{second}\n")
    out = tmp_path / "out.jsonl"
    out.write_text('{"tx_hash": "' + first + '", "ans')  # a run killed while writing
    CliRunner().invoke(cli.app, ["batch", str(hashes), "--config", str(ROOT / "configs" / "ethereum-mainnet.yaml"),
                                 "--out", str(out)])
    rows = [json.loads(line) for line in out.read_text().splitlines()[1:]]  # the cut line stays, alone
    assert [r["tx_hash"].lower() for r in rows] == [first, second]  # the same hash in two cases: once
