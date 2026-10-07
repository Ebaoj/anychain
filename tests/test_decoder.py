from anychain.decoder import AbiDecoder, format_value
from tests.conftest import SWAP_TX, recorded_body

ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
PAIR = "0x8df7C13961a8E5d5f3ee17b942efc09b8CFd19c3"


def _abi(address):
    return recorded_body("eth_uniswap_v2_swap", f"/smart-contracts/{address}")["abi"]


def test_decodes_router_swap_call():
    tx = recorded_body("eth_uniswap_v2_swap", SWAP_TX)
    call = AbiDecoder(_abi(ROUTER)).decode_call(tx["raw_input"])
    assert call.name == "swapExactETHForTokensSupportingFeeOnTransferTokens"
    args = {a.name: a.value for a in call.args}
    assert args["amountOutMin"] == "0"
    assert args["to"] == "0x46c8AC35FdfA77fCfE09C3b92C58761E49543e1b"


def test_unknown_or_garbage_calldata_returns_none():
    decoder = AbiDecoder(_abi(ROUTER))
    assert decoder.decode_call("0xdeadbeef") is None
    assert decoder.decode_call("0xzz") is None
    assert decoder.decode_call("0x12") is None


def test_decodes_pair_swap_event():
    logs = recorded_body("eth_uniswap_v2_swap", "/logs")["items"]
    decoder = AbiDecoder(_abi(PAIR))
    names = [ev.name for log in logs if (ev := decoder.decode_log([t for t in log["topics"] if t], log["data"]))]
    assert "Swap" in names and "Sync" in names


ERC20_TRANSFER = {"type": "event", "name": "Transfer", "inputs": [
    {"name": "from", "type": "address", "indexed": True},
    {"name": "to", "type": "address", "indexed": True},
    {"name": "value", "type": "uint256", "indexed": False}]}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def test_event_with_wrong_indexed_count_is_not_misdecoded():
    # ERC-721 shape: 3 indexed topics and empty data
    topics = [TRANSFER_TOPIC, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + "00" * 31 + "01"]
    assert AbiDecoder([ERC20_TRANSFER]).decode_log(topics, "0x") is None


def test_topic_that_does_not_fit_the_type_is_not_a_crash():
    bad_address_topic = "0x" + "ff" * 32  # 32 non-zero bytes cannot be an address
    topics = [TRANSFER_TOPIC, bad_address_topic, "0x" + "00" * 32]
    assert AbiDecoder([ERC20_TRANSFER]).decode_log(topics, "0x" + "00" * 32) is None


def test_values_are_formatted_by_abi_type_not_by_shape():
    looks_like_address = "0x" + "ab" * 20  # a string argument, not an address
    assert format_value("string", looks_like_address) == looks_like_address
    assert format_value("string", "0x" + "zz" * 20) == "0x" + "zz" * 20
    assert format_value("address", "0x" + "ab" * 20) == "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"
    assert format_value("bytes32", b"\x01" * 32) == "0x" + "01" * 32
    assert format_value("address[]", ["0x" + "00" * 20]) == "[0x0000000000000000000000000000000000000000]"


def test_indexed_dynamic_value_is_labelled_as_a_hash():
    abi = [{"type": "event", "name": "Note", "inputs": [{"name": "text", "type": "string", "indexed": True}]}]
    from eth_utils import event_signature_to_log_topic
    topic0 = "0x" + event_signature_to_log_topic("Note(string)").hex()
    event = AbiDecoder(abi).decode_log([topic0, "0x" + "11" * 32], "0x")
    assert event.args[0].value.endswith("(hash of the string value)")
