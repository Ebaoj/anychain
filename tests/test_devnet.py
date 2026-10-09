"""PHASE4 T6 (D60): the private demo network, recorded on 2026-10-08 (Anvil + a self-hosted Blockscout on home server,
the BRLC token from the configured repo deployed behind a proxy). Retargeting needed configs/devnet.yaml only."""
from anychain.config import load_config
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

CFG = load_config(str(ROOT / "configs" / "devnet.yaml"))


def _bundle(fixture):
    _c, tx = _case(fixture)
    return replay_bundle(CFG, tx, fixture)


def test_the_brlc_call_is_decoded_from_the_configured_repository():
    call = next(e for e in _bundle("devnet_fail_access").items if e.kind == "call")
    assert "setPauser(address)" in call.text and "repository cloudwallk/brlc-token@74a5498" in call.text


def test_an_access_control_reason_from_the_replay_becomes_a_likely_cause():
    # real: Blockscout run without the internal-transactions fetcher reports no reason; the replay on the node at the
    # parent block reverted with OpenZeppelin v4's "Ownable: caller is not the owner", and no conclusion came of it
    # because the replay's table of meanings had no access-control entry
    b = _bundle("devnet_fail_access")
    replay = next(e for e in b.items if e.kind == "diagnosis" and e.data.get("from_replay"))
    assert "'Ownable: caller is not the owner'" in replay.text and replay.data["label"] == "LIKELY"


def test_the_allowance_failure_from_the_replay():
    b = _bundle("devnet_fail_allowance")
    replay = next(e for e in b.items if e.kind == "diagnosis" and e.data.get("from_replay"))
    assert "'ERC20: insufficient allowance'" in replay.text


def test_a_brlc_transfer_in_the_tokens_units():
    t = next(e for e in _bundle("devnet_transfer").items if e.kind == "token_transfer")
    assert "100 BRLC" in t.text
