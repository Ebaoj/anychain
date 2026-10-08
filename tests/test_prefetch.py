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


def test_abi_lookups_do_not_wait_for_a_slow_internal_call_list(monkeypatch, eth_cfg):
    # Live, 2026-10-08: eth.blockscout.com took 19 s for one transaction's internal calls while its
    # contract metadata took 0.5 s; waiting for the list cost the ABI lookups their time budget.
    # The internal call list here only answers after an ABI lookup has started (or gives up after 2 s);
    # an Event, not time.sleep, which the suite replaces with a no-op.
    import threading

    from anychain.collectors.explorer import ExplorerClient
    from tests.conftest import USDC_TX
    abi_started = threading.Event()
    real_internal, real_contract = ExplorerClient.internal_transactions, ExplorerClient.smart_contract
    seen_before_internal = []

    def slow_internal(self, tx_hash):
        seen_before_internal.append(abi_started.wait(timeout=2))
        return real_internal(self, tx_hash)

    def contract(self, address):
        abi_started.set()
        return real_contract(self, address)
    monkeypatch.setattr(ExplorerClient, "internal_transactions", slow_internal)
    monkeypatch.setattr(ExplorerClient, "smart_contract", contract)
    replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    assert seen_before_internal and seen_before_internal[0] is True


def test_the_same_request_in_flight_is_made_once(eth_cfg):
    # Four threads ask for the same thing while the first request is still running (held open by
    # an Event, not time.sleep, which the suite replaces with a no-op): one request, four answers.
    import threading

    from anychain.collectors.explorer import ExplorerClient
    client = ExplorerClient(eth_cfg.explorer)
    calls, gate = [], threading.Event()

    def fetch():
        calls.append(1)
        gate.wait(timeout=2)
        return "value"
    results = []
    threads = [threading.Thread(target=lambda: results.append(client._once(("k",), fetch))) for _ in range(4)]
    for t in threads:
        t.start()
    threading.Event().wait(0.2)  # let every thread reach _once while the first fetch is held open
    gate.set()
    for t in threads:
        t.join()
    assert calls == [1] and results == ["value"] * 4
