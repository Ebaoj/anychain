"""PHASE4 T3 (R3, D57): gas notes, always heuristic, on real recordings."""
from anychain.config import load_config
from anychain.gas import storage_in_loop
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case


def _bundle(fixture):
    c, tx = _case(fixture)
    return replay_bundle(load_config(str(ROOT / "configs" / f"{c}.yaml")), tx, fixture)


def _note(b):
    return next(e for e in b.items if e.kind == "gas_note")


def test_the_share_of_the_limit_used():
    note = _note(_bundle("eth_uniswap_v2_swap"))
    assert "Gas used 131767 of the 300000 limit (43.9%)" in note.text and "more than twice" in note.text
    assert note.text.startswith("Gas notes (heuristic, not a finding)")


def test_the_same_calls_successful_attempt_from_the_timeline():
    # real: the custom-error failure had a limit of 777776; the sender's success of the same call had 845338
    b = _bundle("eth_fail_custom_error")
    note = _note(b)
    timeline = next(e for e in b.items if e.kind == "timeline" and e.data.get("rows"))
    assert "using 626697 gas, with a limit of 845338" in note.text and timeline.id in note.text
    assert "this transaction had a limit of 777776" in note.text


def test_a_write_to_a_different_slot_each_iteration_is_not_flagged():
    # real: ValidatorTimelock line 208, committedBatchTimestamp[_chainAddress].set(i, timestamp) inside the loop
    assert "storage variable" not in _note(_bundle("eth_blob")).text


def test_a_plain_storage_read_inside_a_loop_is_flagged():
    import httpx
    from anychain.bundle import BundleBuilder
    from anychain.collectors.explorer import ExplorerClient
    from anychain.collectors.rpc import RpcClient
    from tests.conftest import make_transport
    c, tx = _case("eth_blob")
    cfg = load_config(str(ROOT / "configs" / f"{c}.yaml"))
    client = httpx.Client(transport=make_transport("eth_blob", None, None))
    builder = BundleBuilder(cfg, ExplorerClient(cfg.explorer, client), RpcClient(cfg.rpc, client))
    builder.build(tx)
    verified, found, name, _address = builder.called_code
    path = found.contract.path
    original = "committedBatchTimestamp[_chainAddress].set(i, timestamp);"
    assert original in verified.uncommented[path]  # the real line
    # the same real function with one edit: the loop body reads the storage variable as a whole
    verified.uncommented[path] = verified.uncommented[path].replace(original, "uint256 x = committedBatchTimestamp;")
    notes = storage_in_loop(verified, found, name)
    assert notes and notes[0][1].startswith("line 208 uses the storage variable committedBatchTimestamp")


def test_no_gas_note_without_gas_data():
    from anychain.gas import usage_note
    assert usage_note(None, 100, "success") is None and usage_note(10, None, "failed") is None
