"""Typed views of explorer and RPC answers, built once at the edge.

The rest of the code reads `tx.to.address` instead of `(tx.get("to") or {}).get("hash")`.
Parsing never raises on an odd field: a value that does not fit becomes None (unknown),
and the bundle decides what to say about it. Field-name variations between Blockscout
versions are handled here and nowhere else.
"""
from dataclasses import dataclass, field
from typing import Any


def to_int(value: object) -> int | None:
    """Parse an int from a decimal string, a 0x-hex string or an int. None if impossible."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class AddressRef:
    """An address as the explorer shows it: hash, name, whether it has code, proxy implementations."""

    address: str | None  # None when the explorer sent an entry without a usable hash
    name: str | None = None
    is_contract: bool | None = None  # today's state, not the state at the transaction's block
    implementations: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, value: object) -> "AddressRef | None":
        """An explorer address entry (a dict). None when absent; address None when unreadable."""
        if not isinstance(value, dict) or not value:
            return None
        impls = tuple(i["address_hash"] for i in _list(value.get("implementations"))
                      if isinstance(i, dict) and isinstance(i.get("address_hash"), str))
        is_contract = value.get("is_contract")
        return cls(address=_str(value.get("hash")) or None,
                   name=_str(value.get("name")) or _str(value.get("ens_domain_name")),
                   is_contract=is_contract if isinstance(is_contract, bool) else None,
                   implementations=impls)


@dataclass(frozen=True)
class Token:
    address: str | None
    symbol: str | None
    name: str | None
    decimals: int | None

    @classmethod
    def from_api(cls, value: object) -> "Token":
        v = _dict(value)
        return cls(_str(v.get("address_hash")), _str(v.get("symbol")), _str(v.get("name")), to_int(v.get("decimals")))

    @property
    def label(self) -> str:
        return self.symbol or self.name or self.address or "unknown token"


@dataclass(frozen=True)
class Authorization:
    """One EIP-7702 authorization: `authority` asks to run the code of `delegate`."""

    authority: str | None
    delegate: str | None  # None when the explorer does not report it (never read as "cleared")
    status: str | None  # "ok" means it took effect; anything else did not

    @classmethod
    def from_api(cls, value: dict) -> "Authorization":
        # Blockscout calls the delegate `address_hash`; older versions call it `address`.
        delegate = value.get("address_hash") or value.get("address")
        return cls(_str(value.get("authority")), _str(delegate), _str(value.get("status")))


@dataclass(frozen=True)
class RevertReason:
    """Why a transaction failed, as the explorer reports it: decoded, raw bytes, or text."""

    method_call: str | None  # e.g. "Error(string reason)" when decoded
    parameters: tuple[tuple[str, object], ...]
    raw_data: str | None  # undecoded revert bytes, "0x" when the revert carried no data
    original: Any  # exactly what the explorer sent (dict or text), kept in the evidence data

    @classmethod
    def from_api(cls, value: object) -> "RevertReason | None":
        if not value:
            return None
        if isinstance(value, dict):
            params = tuple((str(p.get("name")), p.get("value")) for p in _list(value.get("parameters"))
                           if isinstance(p, dict))
            return cls(_str(value.get("method_call")) or None, params, _str(value.get("raw")), value)
        return cls(None, (), None, value)

    @property
    def carried_no_data(self) -> bool:
        """The revert happened with empty data: no reason string, no custom error."""
        return self.method_call is None and isinstance(self.original, dict) and self.raw_data in ("0x", "")

    @property
    def reported(self) -> bool:
        """False for shapes that say nothing, such as {"raw": null}."""
        return not (isinstance(self.original, dict) and self.method_call is None and not self.raw_data)

    def describe(self) -> str:
        if self.carried_no_data:
            return "no revert data"
        if self.method_call:
            params = ", ".join(f"{name}={value!r}" for name, value in self.parameters)
            return f"{self.method_call} with {params}" if params else self.method_call
        if self.raw_data:
            return f"undecoded revert data {self.raw_data}"
        return repr(self.original)


@dataclass(frozen=True)
class Transaction:
    """A transaction from the explorer. `raw` keeps the full answer for chain profiles and audits."""

    status: str | None  # Blockscout: "ok", "error", or None while pending
    result: Any  # Blockscout's free-text result ("success", "Reverted", "out of gas", ...)
    block_number: int | None
    timestamp: str | None
    sender: AddressRef | None
    to: AddressRef | None
    created_contract: AddressRef | None
    value: int | None
    gas_used: int | None
    gas_limit: int | None
    raw_input: str | None  # None when absent or not text: unreadable, never assumed empty
    revert_reason: RevertReason | None
    authorizations: tuple[Authorization, ...]
    authorizations_readable: bool  # False when authorization_list is present but not a list
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, v: dict) -> "Transaction":
        auth_list = v.get("authorization_list")
        return cls(
            status=_str(v.get("status")),
            result=v.get("result"),
            block_number=to_int(v.get("block_number")),
            timestamp=_str(v.get("timestamp")),
            sender=AddressRef.from_api(v.get("from")),
            to=AddressRef.from_api(v.get("to")),
            created_contract=AddressRef.from_api(v.get("created_contract")),
            value=to_int(v.get("value")),
            gas_used=to_int(v.get("gas_used")),
            gas_limit=to_int(v.get("gas_limit")),
            # Blockscout always sends raw_input ("0x" when empty), so absent means unreadable, not empty.
            raw_input=_str(v.get("raw_input")),
            revert_reason=RevertReason.from_api(v.get("revert_reason")),
            authorizations=tuple(Authorization.from_api(a) for a in _list(auth_list) if isinstance(a, dict)),
            authorizations_readable=auth_list is None or isinstance(auth_list, list),
            raw=v,
        )


@dataclass(frozen=True)
class TokenTransfer:
    token: Token
    sender: AddressRef | None
    recipient: AddressRef | None
    log_index: int | None
    value: int | None  # amount in the token's smallest units, or an ERC-1155 quantity
    decimals: int | None  # the transfer's decimals, else the token's
    token_id: object  # ERC-721/1155 token id, as reported

    @classmethod
    def from_api(cls, v: dict) -> "TokenTransfer":
        token, total = Token.from_api(v.get("token")), _dict(v.get("total"))
        decimals = to_int(total.get("decimals")) if total.get("decimals") is not None else token.decimals
        return cls(token, AddressRef.from_api(v.get("from")), AddressRef.from_api(v.get("to")),
                   to_int(v.get("log_index")), to_int(total.get("value")), decimals, total.get("token_id"))


@dataclass(frozen=True)
class InternalCall:
    type: str | None  # call, delegatecall, staticcall, create, create2, selfdestruct, ...
    sender: AddressRef | None
    recipient: AddressRef | None
    created_contract: AddressRef | None
    value: int  # 0 when absent
    success: bool | None  # None when the explorer does not say

    @classmethod
    def from_api(cls, v: dict) -> "InternalCall":
        success = v.get("success")
        return cls(_str(v.get("type")), AddressRef.from_api(v.get("from")), AddressRef.from_api(v.get("to")),
                   AddressRef.from_api(v.get("created_contract")), to_int(v.get("value")) or 0,
                   success if isinstance(success, bool) else None)


@dataclass(frozen=True)
class Log:
    index: int | None
    emitter: AddressRef | None
    topics: tuple[str, ...]  # empty slots removed
    data: str

    @classmethod
    def from_api(cls, v: dict) -> "Log":
        return cls(to_int(v.get("index")), AddressRef.from_api(v.get("address")),
                   tuple(t for t in _list(v.get("topics")) if isinstance(t, str) and t), _str(v.get("data")) or "0x")


@dataclass(frozen=True)
class RpcTransaction:
    """eth_getTransactionByHash. Addresses stay as the node sent them (lowercase)."""

    sender: str | None
    to: str | None
    value: int | None
    input: str | None  # None when absent or not text: unreadable, never assumed empty
    block_number: int | None
    gas: int | None
    delegates: tuple[str, ...]  # EIP-7702 targets; the node does not say who signed them

    @classmethod
    def from_rpc(cls, v: dict) -> "RpcTransaction":
        # JSON-RPC always includes `input` ("0x" when empty): absent means unreadable, not empty.
        return cls(_str(v.get("from")), _str(v.get("to")), to_int(v.get("value")), _str(v.get("input")),
                   to_int(v.get("blockNumber")), to_int(v.get("gas")),
                   tuple(str(a.get("address")) for a in _list(v.get("authorizationList")) if isinstance(a, dict)))


@dataclass(frozen=True)
class RpcReceipt:
    """eth_getTransactionReceipt."""

    status: str  # "success", "failed", or "unknown" for receipts without a status field (very old ones)
    gas_used: int | None
    log_count: int
    contract_address: str | None

    @classmethod
    def from_rpc(cls, v: dict) -> "RpcReceipt":
        status = {"0x1": "success", "0x0": "failed"}.get(v.get("status"), "unknown")
        return cls(status, to_int(v.get("gasUsed")), len(_list(v.get("logs"))), _str(v.get("contractAddress")))
