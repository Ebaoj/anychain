"""The parallel prefetch (D26) changes speed only: same requests, same answer, on every real recording."""
import json
from collections import Counter

import pytest

from anychain.bundle import BundleBuilder
from anychain.config import load_config
from tests.conftest import ROOT, make_transport, replay_bundle
from tests.test_golden import FIXTURE_NAMES, _case


def _run(fixture: str, prefetch: bool, monkeypatch):
    config, tx_hash = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    explorer_host = cfg.explorer.base_url.split("//")[1]
    offline = {explorer_host} if "rpc_only" in fixture else None
    transport = make_transport(fixture, offline, None)
    with monkeypatch.context() as local:  # only this patch is undone; the suite's own fixtures stay
        if not prefetch:
            local.setattr(BundleBuilder, "_prefetch", lambda self, h, tx: None)
        bundle = replay_bundle(cfg, tx_hash, fixture, transport=transport)
    return json.loads(bundle.model_dump_json()), Counter(transport.calls)  # counts: no request made twice


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_prefetch_asks_nothing_extra_and_changes_no_answer(fixture, monkeypatch):
    sequential, asked_sequential = _run(fixture, False, monkeypatch)
    parallel, asked_parallel = _run(fixture, True, monkeypatch)
    assert parallel == sequential
    assert asked_parallel == asked_sequential


def test_prefetch_skips_internal_calls_the_explorer_is_still_indexing(monkeypatch):
    # Real recording, with the state the explorer reports while it indexes internal calls.
    from anychain.config import load_config
    from tests.conftest import USDC_TX, mutated
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    override = mutated("eth_usdc_transfer", USDC_TX, lambda body: body.update(result="awaiting_internal_transactions"))
    transport = make_transport("eth_usdc_transfer", None, override)
    replay_bundle(cfg, USDC_TX, "eth_usdc_transfer", transport=transport)
    assert not [c for c in transport.calls if "/internal-transactions" in c]
