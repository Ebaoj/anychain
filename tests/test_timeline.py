"""PHASE4 T2 (R2, D56): the sender's transactions around a failure, and the patterns they show.

The recording (eth_fail_timeline, 2026-10-08) is the real Uniswap deadline failure 0x73c4c038… whose sender sent the
same swap again 48 seconds later, successfully. The pattern rules are also run on those real rows with one explicit
edit each (marked), as the other rule tests do with recorded answers."""
import json
from dataclasses import replace

import httpx

from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.replay import ReplayTransport
from anychain.config import load_config
from anychain.timeline import build
from tests.conftest import FIXTURES, ROOT, replay_bundle

ETH = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
TX = "0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a"
SENDER = "0x79D6C25dE86bC4A858d7e383A474667aB109FeD0"


def _rows():
    client = ExplorerClient(ETH.explorer, httpx.Client(transport=ReplayTransport(FIXTURES / "eth_fail_timeline.json")))
    latest, _ = client.sent_page(SENDER)
    before, _ = client.sent_page(SENDER, before_block=26149091 + 1)
    return latest + [r for r in before if r.hash not in {x.hash for x in latest}]


def test_the_failure_and_its_neighbours_by_nonce():
    b = replay_bundle(ETH, TX, "eth_fail_timeline")
    facts = [e for e in b.items if e.kind == "timeline"]
    assert "nonce 52: swapExactETHForTokens" in facts[0].text and "(this transaction)" in facts[0].text
    assert [r["nonce"] for r in facts[0].data["rows"]] == [49, 50, 51, 52, 53, 54, 55]
    assert facts[0].sources[0].url.endswith(f"/addresses/{SENDER}/transactions?filter=from")


def test_the_retry_that_succeeded_is_a_pattern():
    b = replay_bundle(ETH, TX, "eth_fail_timeline")
    pattern = next(e for e in b.items if e.kind == "timeline" and e.data.get("pattern"))
    assert pattern.data["pattern"] == "retried_ok" and "48 seconds later" in pattern.text and "nonce 53" in pattern.text


def test_only_the_senders_own_transactions():
    # the real list has an incoming transaction (nonce 61473, from another account) between the sender's own
    t = build(TX, SENDER, _rows())
    assert all(r.sender.lower() == SENDER.lower() for r in t.rows) and 61473 not in [r.nonce for r in t.rows]


def test_an_approve_between_the_failure_and_the_success():
    rows = [replace(r, method="approve") if r.nonce == 53 else r for r in _rows()]  # edit: nonce 53 an approve
    rows = [replace(r, method="swapExactETHForTokens", to=next(x.to for x in rows if x.nonce == 52))
            if r.nonce == 54 else r for r in rows]  # edit: nonce 54 the same swap
    t = build(TX, SENDER, rows)
    assert [p[0] for p in t.patterns] == ["approved_then_ok"] and "nonce 53" in t.patterns[0][1]


def test_repeated_failures_of_the_same_call():
    rows = [replace(r, result="Reverted") if r.nonce == 53 else r for r in _rows()]  # edit: the retry failed too
    t = build(TX, SENDER, rows)
    assert ("repeated_failures" in [p[0] for p in t.patterns]) and "nonces 52, 53" in t.patterns[-1][1]
    assert "retried_ok" not in [p[0] for p in t.patterns]


def test_no_timeline_for_a_success():
    from tests.test_golden import _case
    _c, tx = _case("eth_usdc_transfer")
    b = replay_bundle(ETH, tx, "eth_usdc_transfer")
    assert not [e for e in b.items if e.kind == "timeline"]


def test_an_explorer_that_cannot_list_the_sender_is_a_gap():
    b = replay_bundle(ETH, TX, "eth_fail_timeline", overrides={
        k.split(" ", 1)[1]: {"status": 503, "body": "busy"}
        for k in json.loads((FIXTURES / "eth_fail_timeline.json").read_text()) if "/addresses/" in k})
    assert not [e for e in b.items if e.kind == "timeline"]
    assert any(g.what == "Timeline" for g in b.gaps)


def test_nothing_is_said_across_nonces_that_were_not_read():
    # real (Celo, 2026-10-08): the pages read held nonces 68277, 68278 and then 68569; the first version called
    # those three failures "in a row" across the ~290 transactions in between that were never read
    from tests.test_golden import _case
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    _c, tx = _case("celo_fail_execution_reverted_no_data")
    b = replay_bundle(cfg, tx, "celo_fail_execution_reverted_no_data")
    texts = " ".join(e.text for e in b.items if e.kind == "timeline")
    assert "68569" not in texts
