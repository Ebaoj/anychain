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


def format_value(abi_type: str, value: object) -> str:
    """Human-safe string for a decoded value, chosen by its ABI type (never by its shape)."""
    if abi_type.endswith("]"):  # array, e.g. address[] or uint256[2]
        inner = abi_type[: abi_type.rindex("[")]
        return "[" + ", ".join(format_value(inner, v) for v in value) + "]"  # type: ignore[union-attr]
    if abi_type.startswith("("):  # tuple: show its parts without guessing their types
        return "(" + ", ".join(_plain(v) for v in value) + ")"  # type: ignore[union-attr]
    if abi_type == "address":
        return to_checksum_address(value)  # type: ignore[arg-type]
    return _plain(value)


def _hex_bytes(text: str) -> bytes | None:
    """'0xabcd' -> bytes, or None when the text is not valid hex."""
    try:
        return bytes.fromhex(text.removeprefix("0x"))
    except (ValueError, AttributeError):
        return None


def _plain(value: object) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(_plain(v) for v in value) + ")"
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
        raw = _hex_bytes(data)
        if raw is None or len(raw) < 4:
            return None
        entry = self.functions.get(raw[:4])
        if entry is None:
            return None
        inputs = entry.get("inputs", [])
        try:
            values = decode([_type_of(i) for i in inputs], raw[4:])
        except Exception:
            return None
        args = [
            DecodedArg(i.get("name") or f"arg{n}", _type_of(i), format_value(_type_of(i), v))
            for n, (i, v) in enumerate(zip(inputs, values))
        ]
        return DecodedCall(entry["name"], signature_of(entry), args)

    def decode_log(self, topics: list[str], data: str) -> DecodedEvent | None:
        topic0, payload = (_hex_bytes(topics[0]) if topics else None), _hex_bytes(data)
        if topic0 is None or payload is None:
            return None
        entry = self.events.get(topic0)
        if entry is None:
            return None
        inputs = entry.get("inputs", [])
        indexed = [i for i in inputs if i.get("indexed")]
        plain = [i for i in inputs if not i.get("indexed")]
        if len(topics) - 1 != len(indexed):
            return None  # same name, different shape (e.g. ERC-20 vs ERC-721 Transfer)
        try:
            plain_vals = decode([_type_of(i) for i in plain], payload)
        except Exception:
            return None
        topic_values = iter(topics[1:])
        plain_values = iter(plain_vals)
        args = []
        for n, item in enumerate(inputs):
            t = _type_of(item)
            name = item.get("name") or f"arg{n}"
            if not item.get("indexed"):
                args.append(DecodedArg(name, t, format_value(t, next(plain_values))))
                continue
            topic = next(topic_values)
            if t in ("string", "bytes") or t.endswith("]") or t.startswith("("):
                # Dynamic indexed values are stored only as their hash: show it as is.
                args.append(DecodedArg(name, t, f"{topic} (hash of the {t} value)"))
                continue
            try:
                value = decode([t], _hex_bytes(topic) or b"")[0]
            except Exception:
                return None  # topic does not fit the ABI: same name, different contract
            args.append(DecodedArg(name, t, format_value(t, value)))
        return DecodedEvent(entry["name"], signature_of(entry), args)
