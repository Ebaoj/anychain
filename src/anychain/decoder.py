"""ABI decoding of calldata and event logs.

Phase 1 uses ABIs from the explorer only. The cascade (repo artifacts, source
signatures, 4byte, raw) is added in phase 2 behind the same interface.
"""
from dataclasses import dataclass, field

from eth_abi import decode
from eth_utils import (
    event_signature_to_log_topic,
    function_signature_to_4byte_selector,
    to_checksum_address,
)


@dataclass
class DecodedArg:
    name: str
    type: str
    value: str


@dataclass
class DecodedCall:
    name: str
    signature: str
    args: list[DecodedArg] = field(default_factory=list)


@dataclass
class DecodedEvent:
    name: str
    signature: str
    args: list[DecodedArg] = field(default_factory=list)


def _type_of(item: dict) -> str:
    """Canonical type string; expands tuples like (address,uint256)[]."""
    t = item["type"]
    if t.startswith("tuple"):
        inner = ",".join(_type_of(c) for c in item.get("components", []))
        return f"({inner}){t[len('tuple'):]}"
    return t


def signature_of(entry: dict) -> str:
    return f"{entry['name']}({','.join(_type_of(i) for i in entry.get('inputs', []))})"


def format_value(value: object) -> str:
    """Human-safe string for a decoded ABI value."""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    if isinstance(value, str) and value.startswith("0x") and len(value) == 42:
        return to_checksum_address(value)
    return str(value)


class AbiDecoder:
    def __init__(self, abi: list[dict]):
        self.functions = {}
        self.events = {}
        for entry in abi:
            if entry.get("type") == "function":
                sel = function_signature_to_4byte_selector(signature_of(entry))
                self.functions[sel] = entry
            elif entry.get("type") == "event":
                topic = event_signature_to_log_topic(signature_of(entry))
                self.events[topic] = entry

    def decode_call(self, data: str) -> DecodedCall | None:
        raw = bytes.fromhex(data.removeprefix("0x"))
        entry = self.functions.get(raw[:4])
        if entry is None:
            return None
        inputs = entry.get("inputs", [])
        try:
            values = decode([_type_of(i) for i in inputs], raw[4:])
        except Exception:
            return None
        args = [DecodedArg(i.get("name") or f"arg{n}", _type_of(i), format_value(v)) for n, (i, v) in enumerate(zip(inputs, values))]
        return DecodedCall(entry["name"], signature_of(entry), args)

    def decode_log(self, topics: list[str], data: str) -> DecodedEvent | None:
        if not topics:
            return None
        entry = self.events.get(bytes.fromhex(topics[0].removeprefix("0x")))
        if entry is None:
            return None
        inputs = entry.get("inputs", [])
        indexed = [i for i in inputs if i.get("indexed")]
        plain = [i for i in inputs if not i.get("indexed")]
        if len(topics) - 1 != len(indexed):
            return None  # same name, different shape (e.g. ERC-20 vs ERC-721 Transfer)
        try:
            plain_vals = decode([_type_of(i) for i in plain], bytes.fromhex(data.removeprefix("0x")))
        except Exception:
            return None
        topic_values = iter(topics[1:])
        plain_values = iter(plain_vals)
        args = []
        for n, item in enumerate(inputs):
            t = _type_of(item)
            if item.get("indexed"):
                topic = next(topic_values)
                dynamic = t in ("string", "bytes") or t.endswith("]") or t.startswith("(")
                value = topic if dynamic else decode([t], bytes.fromhex(topic.removeprefix("0x")))[0]
            else:
                value = next(plain_values)
            args.append(DecodedArg(item.get("name") or f"arg{n}", t, format_value(value)))
        return DecodedEvent(entry["name"], signature_of(entry), args)
