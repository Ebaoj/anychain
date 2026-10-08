"""PHASE2 T7 (R5): public signature database candidates, from real recorded 4byte answers."""
import json

import httpx
import pytest

from anychain.collectors.http import CollectorError
from anychain.collectors.signatures import EMPTY_TTL_S, SignatureDb
from anychain.config import SignatureDbConfig, load_config
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

DB = SignatureDbConfig(enabled=True, url="https://www.4byte.directory/api/v1")


def _bundle(fixture):
    config, tx = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    offline = {cfg.explorer.base_url.split("//")[1]} if "rpc_only" in fixture else None
    return replay_bundle(cfg, tx, fixture, offline_hosts=offline)


def test_function_match_that_fits_is_a_candidate_never_the_call():
    # Explorer down: the node's call data, selector 0xa9059cbb, against 4byte's real entries (all pages): two
    # of its six texts fit a transfer's data exactly, so neither is chosen.
    b = _bundle("eth_usdc_rpc_only")
    [c] = [e for e in b.items if e.kind == "candidate"]
    assert c.confidence == "candidate" and not c.text.startswith("Called") and "not confirmed" in c.text
    assert c.data["fitting"] == ["transfer(address,uint256)", "workMyDirefulOwner(uint256,uint256)"]
    assert "Which one, if any, is not known" in c.text
    assert c.sources[0].kind == "signature_db" and "hex_signature=0xa9059cbb" in c.sources[0].url


def test_event_signature_is_proven_by_its_hash():
    b = _bundle("eth_dsproxy_recipe")
    proven = [e for e in b.items if e.kind == "event_signature"]
    assert proven and all(e.confidence == "single_source" and "its hash was checked" in e.text for e in proven)


def _fake(monkeypatch, functions=None, events=None, error=None):
    def lookup(answers):
        def go(self, key):
            if error:
                raise error
            return (answers or {}).get(key, [])
        return go
    monkeypatch.setattr(SignatureDb, "functions", lookup(functions))
    monkeypatch.setattr(SignatureDb, "events", lookup(events))
    monkeypatch.setattr(SignatureDb, "enabled", property(lambda self: True))


def test_an_event_text_whose_hash_differs_is_discarded(monkeypatch):
    topics = [e.data["topic"] for e in _bundle("eth_dsproxy_recipe").items if e.kind == "event_signature"]
    _fake(monkeypatch, events={t: ["Spoofed(uint256)"] for t in topics})
    b = _bundle("eth_dsproxy_recipe")
    assert not [e for e in b.items if e.kind == "event_signature"]


def test_only_signatures_the_data_fits_are_offered(monkeypatch):
    # The database lists texts whose selector matches; only those whose types the call data fits exactly
    # are offered (two texts with the same 4-byte selector would both have to fit to be listed together).
    _fake(monkeypatch, functions={"0xa9059cbb": ["transfer(address,uint256)", "transfer(bytes4[9],bytes5[6],int48[11])"]})
    [c] = [e for e in _bundle("eth_usdc_rpc_only").items if e.kind == "candidate"]
    assert c.data["fitting"] == ["transfer(address,uint256)"]  # the second does not fit the data
    _fake(monkeypatch, functions={"0xa9059cbb": ["transfer(bytes4[9],bytes5[6],int48[11])"]})
    [c] = [e for e in _bundle("eth_usdc_rpc_only").items if e.kind == "candidate"]
    assert c.data["fitting"] == [] and "none of them is offered" in c.text


def test_a_failed_lookup_is_a_gap(monkeypatch):
    _fake(monkeypatch, error=CollectorError("timeout", retryable=True))
    b = _bundle("eth_usdc_rpc_only")
    assert any(g.what == "Signature database" and g.cause == "source_unavailable" for g in b.gaps)


def test_answers_are_cached_on_disk(tmp_path):
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"count": 2, "next": None, "results": [
            {"id": 2, "text_signature": "b()", "hex_signature": "0x12345678"},
            {"id": 1, "text_signature": "a()", "hex_signature": "0x12345678"}]})
    db = _hand_db(tmp_path, handler)
    assert db.functions("0x12345678") == ["a()", "b()"]  # oldest submission first
    assert db.functions("0x12345678") == ["a()", "b()"] and len(asked) == 1
    assert json.loads(db.path.read_text())["function:0x12345678"]["signatures"] == ["a()", "b()"]


def test_empty_answers_are_asked_again_after_a_week(tmp_path):
    asked = []
    db = _hand_db(tmp_path, lambda r: asked.append(1) or httpx.Response(200, json={"count": 0, "next": None,
                                                                                    "results": []}))
    db.functions("0xaaaaaaaa")
    db.functions("0xaaaaaaaa")
    assert len(asked) == 1
    cache = json.loads(db.path.read_text())
    cache["function:0xaaaaaaaa"]["at"] -= EMPTY_TTL_S + 1
    db.path.write_text(json.dumps(cache))
    db.functions("0xaaaaaaaa")
    assert len(asked) == 2


# ---- cases from the clean-context review of T7 (written before the fixes) ---------------------------

def _hand_db(tmp_path, handler, cfg=DB):
    import threading
    db = SignatureDb.__new__(SignatureDb)  # built by hand: the suite replaces __init__ to stay offline
    db.cfg, db.client, db.timeout_s, db.offline = cfg, httpx.Client(transport=httpx.MockTransport(handler)), 10, False
    db.path, db._lock, db.failed = tmp_path / "signatures.json", threading.Lock(), False
    from anychain.collectors.http import Budget
    db.budget = Budget(8)
    return db


def test_every_page_is_read_and_ordered_oldest_first(tmp_path):
    pages = {None: {"count": 3, "next": "https://www.4byte.directory/api/v1/signatures/?page=2",
                    "results": [{"id": 1, "text_signature": "a()", "hex_signature": "0x12345678"},
                                {"id": 2, "text_signature": "b()", "hex_signature": "0x12345678"}]},
             "2": {"count": 3, "next": None, "results": [{"id": 3, "text_signature": "c()", "hex_signature": "0x12345678"}]}}
    asked = []

    def handler(request):
        asked.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("page")])
    db = _hand_db(tmp_path, handler)
    assert db.functions("0x12345678") == ["a()", "b()", "c()"]
    assert asked[0].get("ordering") == "created_at" and len(asked) == 2


def test_only_exact_selector_matches_are_kept(tmp_path):
    # 4byte's hex_signature filter is a prefix match (checked live by the review)
    def handler(request):
        return httpx.Response(200, json={"count": 2, "next": None, "results": [
            {"id": 1, "text_signature": "x()", "hex_signature": "0x12345678"},
            {"id": 2, "text_signature": "y()", "hex_signature": "0x123456789"}]})
    assert _hand_db(tmp_path, handler).functions("0x12345678") == ["x()"]


@pytest.mark.parametrize("key", ["0xa9059c", "0x12", "0x" + "1" * 63])
def test_malformed_keys_are_never_looked_up(tmp_path, key):
    asked = []
    db = _hand_db(tmp_path, lambda r: asked.append(1) or httpx.Response(200, json={"count": 0, "results": []}))
    assert (db.events(key) if len(key) > 20 else db.functions(key)) == [] and asked == []


def test_disabled_in_config_means_no_candidates(monkeypatch):
    from anychain.collectors import signatures as sig_module
    real = sig_module.SignatureDb.__init__

    def disabled(self, cfg, cache_dir, *a, **k):
        real(self, cfg.model_copy(update={"enabled": False}), cache_dir, *a, **k)
    monkeypatch.setattr(sig_module.SignatureDb, "__init__", disabled)  # on top of the suite's offline cache
    b = _bundle("eth_usdc_rpc_only")
    assert not [e for e in b.items if e.kind == "candidate"]


def test_after_one_failure_no_more_lookups_and_one_gap(monkeypatch):
    calls = []

    def boom(self, key):
        calls.append(key)
        raise CollectorError("timeout", retryable=True)
    monkeypatch.setattr(SignatureDb, "_fetch_all", lambda self, endpoint, key: boom(self, key))
    monkeypatch.setattr(SignatureDb, "_read", lambda self: {})
    suite_init = SignatureDb.__init__

    def online(self, *a, **k):
        suite_init(self, *a, **k)
        self.offline = False  # the stubbed fetch is the only "network"
    monkeypatch.setattr(SignatureDb, "__init__", online)
    b = _bundle("eth_dsproxy_recipe")  # several undecoded event topics
    assert len(calls) == 1 and len([g for g in b.gaps if g.what == "Signature database"]) == 1


def test_two_fitting_texts_are_both_listed(monkeypatch):
    # Real 4byte entries for 0xa9059cbb include workMyDirefulOwner(uint256,uint256), which fits a transfer's data too.
    _fake(monkeypatch, functions={"0xa9059cbb": ["transfer(address,uint256)", "workMyDirefulOwner(uint256,uint256)"]})
    [c] = [e for e in _bundle("eth_usdc_rpc_only").items if e.kind == "candidate"]
    assert c.data["fitting"] == ["transfer(address,uint256)", "workMyDirefulOwner(uint256,uint256)"]
    assert "Which one, if any, is not known" in c.text


def test_oracle_finds_an_address_only_as_a_whole_word():
    from anychain.models import EvidenceBundle
    from anychain.oracle import _check_addresses_exist
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    addr = "74aa5387681505c806ff1e972b12cdfd01406828"
    b = EvidenceBundle(network="n", tx_hash="0x1", status="success")
    b.add("call", f"arg0=0x{addr}", [])
    assert _check_addresses_exist(b, cfg, "0xa9059cbb" + "0" * 24 + addr).status == "pass"   # a padded word
    assert _check_addresses_exist(b, cfg, "0xdeadbeef" + addr + "ff" * 12).status == "fail"  # a misaligned slice
