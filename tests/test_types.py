"""The typed objects built at the edge: odd explorer and RPC shapes become None ("unknown"),
never an invented value. Shapes taken from a differential review (13,151 mutated cases)."""
import pytest

from anychain.collectors.types import (
    AddressRef, Authorization, InternalCall, Log, RevertReason, RpcReceipt, RpcTransaction, TokenTransfer,
    Transaction, to_int,
)


@pytest.mark.parametrize("value,expected", [
    ("26141501", 26141501), ("0x1f", 31), (7, 7), (True, None), ("abc", None), (None, None), (3.5, None),
])
def test_to_int(value, expected):
    assert to_int(value) == expected


def test_address_entry_without_hash_is_unknown_not_invented():
    assert AddressRef.from_api({"hash": None, "name": "X"}).address is None
    assert AddressRef.from_api({}) is None and AddressRef.from_api(None) is None
    assert AddressRef.from_api("0xabc") is None  # explorer entries are objects; a bare string is not trusted
    ref = AddressRef.from_api({"hash": "0xA", "is_contract": "yes", "implementations": [{"address_hash": "0xB"}, 5]})
    assert ref.is_contract is None and ref.implementations == ("0xB",)


@pytest.mark.parametrize("raw,readable", [(None, None), ("0x", "0x"), ("0x1234", "0x1234"), (True, None), (5, None)])
def test_unreadable_call_data_is_none_not_no_data(raw, readable):
    assert Transaction.from_api({"raw_input": raw}).raw_input == readable
    assert RpcTransaction.from_rpc({"input": raw}).input == readable


def test_authorization_list_readability():
    assert Transaction.from_api({}).authorizations_readable
    assert Transaction.from_api({"authorization_list": []}).authorizations_readable
    assert not Transaction.from_api({"authorization_list": True}).authorizations_readable


def test_authorization_delegate_field_names():
    assert Authorization.from_api({"address_hash": "0xD"}).delegate == "0xD"
    assert Authorization.from_api({"address": "0xD"}).delegate == "0xD"  # older Blockscout
    assert Authorization.from_api({"authority": 5}).authority is None


@pytest.mark.parametrize("value,carried_no_data,reported,text", [
    ({"method_call": "Error(string reason)", "parameters": [{"name": "reason", "value": "EXPIRED"}]},
     False, True, "Error(string reason) with reason='EXPIRED'"),
    ({"raw": "0x1234"}, False, True, "undecoded revert data 0x1234"),
    ({"raw": "0x"}, True, True, "no revert data"),
    ({"method_call": "", "raw": "0x"}, True, True, "no revert data"),  # an empty name means "not decoded"
    ({"method_call": ""}, False, False, "{'method_call': ''}"),
    ({"raw": None}, False, False, "{'raw': None}"),
])
def test_revert_reason_shapes(value, carried_no_data, reported, text):
    reason = RevertReason.from_api(value)
    assert (reason.carried_no_data, reason.reported, reason.describe()) == (carried_no_data, reported, text)


def test_internal_call_success_is_only_a_real_boolean():
    assert InternalCall.from_api({"success": "false"}).success is None
    assert InternalCall.from_api({"success": False}).success is False
    assert InternalCall.from_api({"value": True}).value == 0


def test_token_transfer_odd_shapes_keep_what_is_readable():
    t = TokenTransfer.from_api({"token": "str", "total": "x", "from": 5, "log_index": "3"})
    assert t.token.label == "unknown token" and t.value is None and t.sender is None and t.log_index == 3
    assert TokenTransfer.from_api({"total": {"value": "5", "decimals": True}}).decimals is None


def test_log_topics_drop_empty_slots():
    log = Log.from_api({"topics": ["0xaa", None, "", 5], "data": None, "address": {"hash": None}})
    assert log.topics == ("0xaa",) and log.data == "0x" and log.emitter.address is None


def test_receipt_status_values():
    assert RpcReceipt.from_rpc({"status": "0x1"}).status == "success"
    assert RpcReceipt.from_rpc({"status": "0x0"}).status == "failed"
    assert RpcReceipt.from_rpc({"blockNumber": "0x1"}).status == "unknown"  # very old receipts
