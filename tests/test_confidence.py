"""PHASE2 T2 (R6): every fact says how sure it is, and the reader and the model can see it."""
import json

import pytest

from anychain.config import load_config
from anychain.models import EvidenceBundle, Source
from anychain.render import render_markdown
from anychain.writer import evidence_payload, load_prompt
from tests.conftest import ROOT, USDC_TX, replay_bundle
from tests.test_golden import FIXTURE_NAMES, _case

LEVELS = {"confirmed", "single_source", "candidate"}


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_fact_has_a_confidence_that_follows_its_sources(fixture):
    config, tx_hash = _case(fixture)
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    offline = {cfg.explorer.base_url.split("//")[1]} if "rpc_only" in fixture else None
    bundle = replay_bundle(cfg, tx_hash, fixture, offline_hosts=offline)
    for e in bundle.items:
        assert e.confidence in LEVELS
        only_node = all(s.kind == "rpc" for s in e.sources)
        comparison = e.kind == "cross_check" or "node_l1_status" in e.data  # explorer checked against the node
        expected = "confirmed" if only_node or comparison else "single_source"
        if e.kind == "diagnosis":  # a finding's level is its own: confirmed by a read, the explorer's reason, or a pattern
            expected = e.data["level"]
        assert e.confidence == expected, (e.id, e.text[:80])


def test_candidate_is_only_set_on_purpose():
    b = EvidenceBundle(network="n", tx_hash="0x1", status="success")
    node = Source(kind="rpc", label="JSON-RPC")
    assert b.add("call", "x", [node]).confidence == "confirmed"
    assert b.add("call", "y", [node], confidence="candidate").confidence == "candidate"  # never upgraded


def test_reader_and_model_see_the_confidence(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    md = render_markdown(bundle)
    assert "_(explorer only)_" in md and "_(confirmed by the node)_" in md
    payload = json.loads(evidence_payload(bundle, eth_cfg))
    assert {e["confidence"] for e in payload["evidence"]} == {"confirmed", "single_source"}
    prompt = load_prompt("support", "pt-BR")
    assert "candidate" in prompt and "single_source" in prompt



def test_node_calldata_decoded_with_the_explorer_abi_is_not_confirmed(eth_cfg):
    # The RPC-only path while the explorer is behind (404 for the tx), but it still serves the ABI:
    # the call comes from the node, its meaning from the explorer, so it is not "confirmed".
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer",
                           overrides={f"/transactions/{USDC_TX}": {"status": 404, "body": "{}"}})
    [call] = [e for e in bundle.items if e.kind == "call"]
    assert call.text.startswith("Called transfer(address,uint256)") and call.data.get("function") == "transfer"
    assert {s.kind for s in call.sources} == {"rpc", "explorer_api"} and call.confidence == "single_source"


def test_candidate_render_text():
    b = EvidenceBundle(network="n", tx_hash="0x1", status="success")
    b.add("call", "Selector 0xa9059cbb matches transfer(address,uint256) in a signature database.",
          [Source(kind="explorer_api", label="4byte")], confidence="candidate")
    assert "_(candidate: inferred, not confirmed)_" in render_markdown(b)
