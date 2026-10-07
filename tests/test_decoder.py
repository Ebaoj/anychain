import json

from anychain.decoder import AbiDecoder
from tests.conftest import FIXTURES


def _recorded_abi(fixture: str, address: str) -> list[dict]:
    records = json.loads((FIXTURES / f"{fixture}.json").read_text())
    key = next(k for k in records if k.endswith(f"/smart-contracts/{address}"))
    return json.loads(records[key]["body"])["abi"]


def test_decodes_router_swap_call():
    abi = _recorded_abi("eth_uniswap_v2_swap", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
    records = json.loads((FIXTURES / "eth_uniswap_v2_swap.json").read_text())
    tx = json.loads(next(v["body"] for k, v in records.items() if k.endswith("0x08b39d3ae20b5c09294209ad4c0bf8e6d14bf660f81ffa6696c455911aa46c6a")))
    call = AbiDecoder(abi).decode_call(tx["raw_input"])
    assert call.name == "swapExactETHForTokensSupportingFeeOnTransferTokens"
    args = {a.name: a.value for a in call.args}
    assert args["amountOutMin"] == "0"
    assert args["to"] == "0x46c8AC35FdfA77fCfE09C3b92C58761E49543e1b"


def test_unknown_selector_returns_none():
    abi = _recorded_abi("eth_uniswap_v2_swap", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
    assert AbiDecoder(abi).decode_call("0xdeadbeef") is None


def test_decodes_pair_swap_event():
    abi = _recorded_abi("eth_uniswap_v2_swap", "0x8df7C13961a8E5d5f3ee17b942efc09b8CFd19c3")
    records = json.loads((FIXTURES / "eth_uniswap_v2_swap.json").read_text())
    logs = json.loads(next(v["body"] for k, v in records.items() if k.endswith("/logs")))["items"]
    decoder = AbiDecoder(abi)
    names = [ev.name for log in logs if (ev := decoder.decode_log([t for t in log["topics"] if t], log["data"]))]
    assert "Swap" in names and "Sync" in names


def test_event_with_wrong_indexed_count_is_not_misdecoded():
    erc20_transfer = {"type": "event", "name": "Transfer", "inputs": [
        {"name": "from", "type": "address", "indexed": True},
        {"name": "to", "type": "address", "indexed": True},
        {"name": "value", "type": "uint256", "indexed": False}]}
    topic0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    # ERC-721 shape: 3 indexed topics, empty data
    assert AbiDecoder([erc20_transfer]).decode_log([topic0, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + "00" * 31 + "01"], "0x") is None
