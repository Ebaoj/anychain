"""PHASE2 T3 (R3): read-only state reads with eth_call, pinned to a block, from real node answers."""
import httpx
import pytest

from anychain.collectors.http import CollectorError
from anychain.collectors.replay import ReplayTransport
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from anychain.reads import StateReader, UnreadableState, selector
from tests.conftest import ROOT

READS = ROOT / "tests" / "fixtures_reads"


def _reader(fixture: str, network: str) -> StateReader:
    cfg = load_config(str(ROOT / "configs" / f"{network}.yaml"))
    client = httpx.Client(transport=ReplayTransport(READS / f"{fixture}.json"))
    return StateReader(RpcClient(cfg.rpc, client))


def test_selectors_come_from_the_signatures():
    assert selector("balanceOf(address)") == "0x70a08231"  # the ERC-20 standard's own selector
    assert selector("paused()") == "0x5c975abb"


def test_balance_before_a_failed_transfer():
    # Celo tx 0x9a8b0c69...: transfer of 106500 USDT units reverted with "exceeds balance".
    reader = _reader("celo_balance_exceeded", "celo-mainnet")
    read = reader.erc20_balance("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e",
                                "0x9695b8367fd1bb4800667ff5f35b0cf142f410a0", 78962883)
    assert read.value == 1445 and read.block == 78962883
    assert read.detail == ("eth_call 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e "
                           "balanceOf(0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0) at block 78962883")


def test_replay_reports_the_revert_in_the_nodes_words():
    reader = _reader("celo_balance_exceeded", "celo-mainnet")
    import json
    records = json.loads((READS / "celo_balance_exceeded.json").read_text())
    tx = next(json.loads(v["body"])["result"] for k, v in records.items() if "eth_getTransactionByHash" in k)
    replay = reader.replay(tx["from"], tx["to"], tx["input"], int(tx["value"], 16), 78962883)
    assert replay.reverted and "ERC20: transfer amount exceeds balance" in replay.message
    assert replay.revert_data.startswith(selector("Error(string)"))


def test_rootstock_paused_and_its_revert_format():
    # rskj says "VM Exception while processing transaction: revert paused" (code -32015), not code 3.
    reader = _reader("rootstock_paused", "rootstock-mainnet")
    read = reader.paused("0x5684a06cab22db16d901fee2a5c081b4c91ea40e", 9261663)
    assert read.value is True
    import json
    records = json.loads((READS / "rootstock_paused.json").read_text())
    tx = next(json.loads(v["body"])["result"] for k, v in records.items() if "eth_getTransactionByHash" in k)
    replay = reader.replay(tx["from"], tx["to"], tx["input"], int(tx["value"], 16), 9261663)
    assert replay.reverted and "revert paused" in replay.message


def test_node_without_old_state_is_an_error_not_a_value():
    reader = _reader("ethereum_archive_refused", "ethereum-mainnet")
    with pytest.raises(CollectorError) as err:
        reader.erc20_balance("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                             "0x0000000000000000000000000000000000000001", 23000000)
    assert "Archive requests require a personal token" in str(err.value)


def test_a_contract_that_does_not_answer_the_read_gives_no_value():
    class Fake:
        def __init__(self, raw): self.raw = raw
        def eth_call(self, *a, **k): return self.raw
    for raw in ("0x", "0x" + "00" * 31, "0x" + "00" * 64):
        with pytest.raises(UnreadableState):
            StateReader(Fake(raw)).erc20_balance("0x" + "1" * 40, "0x" + "2" * 40, 1)


# ---- cases from the clean-context review of T3 (written before the fixes) -----------------------

def test_rootstock_revert_without_reason_is_a_revert():
    # rskj: code -32015 "VM Exception while processing transaction: transaction reverted", data "0x"
    # (RIF token has no paused(): the read must say "no answer", not raise a node error)
    reader = _reader("rootstock_no_reason_revert", "rootstock-mainnet")
    with pytest.raises(UnreadableState):
        reader.paused("0x2acc95758f8b5f583470ba265eb685a8f45fc9d5", 9307457)


def test_a_node_error_that_is_not_a_revert_stays_an_error():
    # Celo: -32000 "insufficient funds for gas * price + value": the call never ran
    from anychain.collectors.rpc import CallReverted
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    rpc = RpcClient(cfg.rpc, httpx.Client(transport=ReplayTransport(READS / "celo_insufficient_funds.json")))
    with pytest.raises(CollectorError) as err:
        rpc.eth_call("0x0000000000000000000000000000000000000002", "0x", 79562690,
                     sender="0x9695b8367fd1bb4800667ff5f35b0cf142f410a0", value=10**24)
    assert not isinstance(err.value, CallReverted) and "insufficient funds" in str(err.value)


@pytest.mark.parametrize("code, message, reverted", [
    (3, "execution reverted: ERC20: transfer amount exceeds balance", True),
    (3, "execution reverted", True),
    (3, "intrinsic gas too low", False),            # Gnosis's node (Tenderly) uses code 3 for this
    (-32015, "VM Exception while processing transaction: revert paused", True),
    (-32015, "VM Exception while processing transaction: transaction reverted", True),
    (-32000, "intrinsic gas too low", False),
    (-32003, "insufficient funds for gas * price + value", False),
])
def test_revert_detection_by_code_and_words(code, message, reverted):
    from anychain.collectors.rpc import CallReverted
    body = {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message, "data": "0x"}}
    rpc = RpcClient(load_config(str(ROOT / "configs" / "celo-mainnet.yaml")).rpc,
                    httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    with pytest.raises(CollectorError) as err:
        rpc.eth_call("0x" + "1" * 40, "0x", 1)
    assert isinstance(err.value, CallReverted) is reverted


def test_a_bool_that_is_not_0_or_1_is_unreadable():
    class Fake:
        def eth_call(self, *a, **k): return "0x" + "00" * 31 + "02"
    with pytest.raises(UnreadableState):
        StateReader(Fake()).paused("0x" + "1" * 40, 1)


def test_read_detail_uses_checksummed_addresses():
    class Fake:
        def eth_call(self, *a, **k): return "0x" + "00" * 32
    read = StateReader(Fake()).erc20_balance("0x48065fbbe25f71c9282ddf5e1cd6d6a887483d5e",
                                             "0x9695b8367fd1bb4800667ff5f35b0cf142f410a0", 1)
    assert read.detail == ("eth_call 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e "
                           "balanceOf(0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0) at block 1")


def test_replay_that_does_not_revert_is_inconclusive():
    class Fake:
        def eth_call(self, *a, **k): return "0x"
    replay = StateReader(Fake()).replay("0x" + "1" * 40, "0x" + "2" * 40, "0x", 0, 1)
    assert not replay.reverted and "inconclusive" in replay.message
    assert "gas" in replay.message and "block" in replay.message  # the limits are stated
