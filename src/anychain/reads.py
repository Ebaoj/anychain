"""Read-only state reads with eth_call, pinned to a block (PHASE2 T3, R3).

Each read says what was asked, of which contract, at which block, and the raw answer, so a fact
built from it can cite the exact call. A contract that does not answer a standard read (wrong
length, or it reverts) gives no value: that is reported, never guessed.
"""
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import function_signature_to_4byte_selector, is_address, to_checksum_address

from anychain.collectors.http import CollectorError
from anychain.collectors.rpc import CallReverted, RpcClient


def selector(signature: str) -> str:
    return "0x" + function_signature_to_4byte_selector(signature).hex()


@dataclass(frozen=True)
class Read:
    signature: str  # e.g. "balanceOf(address)"
    contract: str
    args: tuple
    block: int
    value: object  # int for uint256, bool for bool

    @property
    def detail(self) -> str:
        """The call as a source detail: method, target, arguments, block (addresses checksummed)."""
        shown = ", ".join(_shown(a) for a in self.args)
        return f"eth_call {_shown(self.contract)} {self.signature.split('(')[0]}({shown}) at block {self.block}"


def _shown(value: object) -> str:
    return to_checksum_address(value) if isinstance(value, str) and is_address(value) else str(value)


class UnreadableState(Exception):
    """The contract did not answer a standard read (not implemented, or reverted)."""


# What a replay cannot reproduce, stated with every result.
REPLAY_LIMITS = ("run on the parent block's state and context (its block number and time), without the "
                 "transactions that came before it in its own block, and without charging the gas cost (nodes "
                 "still check that the sender holds the value it sends)")


@dataclass(frozen=True)
class Replay:
    """The transaction's call run again with eth_call. Not the original execution (REPLAY_LIMITS).
    A revert reproduced is evidence of the cause; no revert is inconclusive, never "it would work"."""

    block: int
    reverted: bool
    revert_data: str | None  # when reverted
    message: str  # the node's own words


class StateReader:
    def __init__(self, rpc: RpcClient):
        self.rpc = rpc

    def _read(self, contract: str, signature: str, args: tuple, types: list[str], out: str, block: int) -> Read:
        data = selector(signature) + (encode(types, list(args)).hex() if types else "")
        try:
            raw = self.rpc.eth_call(contract, data, block)
        except CallReverted as exc:
            raise UnreadableState(f"{signature} on {contract} reverted at block {block}") from exc
        payload = bytes.fromhex(raw[2:])
        if len(payload) != 32:
            raise UnreadableState(f"{signature} on {contract} answered {len(payload)} bytes, not one {out}")
        try:
            (value,) = decode([out], payload)
        except DecodingError as exc:  # e.g. a "bool" that is neither 0 nor 1
            raise UnreadableState(f"{signature} on {contract} answered {raw}, not a valid {out}") from exc
        return Read(signature, contract, args, block, value)

    def erc20_balance(self, token: str, holder: str, block: int) -> Read:
        return self._read(token, "balanceOf(address)", (holder,), ["address"], "uint256", block)

    def erc20_allowance(self, token: str, owner: str, spender: str, block: int) -> Read:
        return self._read(token, "allowance(address,address)", (owner, spender), ["address", "address"], "uint256",
                          block)

    def owner(self, contract: str, block: int) -> Read:
        return self._read(contract, "owner()", (), [], "address", block)

    def pending_owner(self, contract: str, block: int) -> Read:
        return self._read(contract, "pendingOwner()", (), [], "address", block)

    def has_role(self, contract: str, role: str, account: str, block: int) -> Read:
        read = self._read(contract, "hasRole(bytes32,address)", (bytes.fromhex(role[2:]), account),
                          ["bytes32", "address"], "bool", block)
        return Read(read.signature, read.contract, (role, account), read.block, read.value)  # the role as hex

    def paused(self, contract: str, block: int) -> Read:
        return self._read(contract, "paused()", (), [], "bool", block)

    def replay(self, sender: str, to: str, data: str, value: int, block: int, gas: int | None = None) -> Replay:
        """Raises InsufficientFunds when the sender does not hold `value` at `block` (not a revert)."""
        try:
            self.rpc.eth_call(to, data, block, sender=sender, value=value, gas=gas)
        except CallReverted as exc:
            return Replay(block, True, exc.data, f"{exc} (replay {REPLAY_LIMITS})")
        return Replay(block, False, None, f"inconclusive: the call did not revert when run again ({REPLAY_LIMITS})")


__all__ = ["CollectorError", "Read", "Replay", "StateReader", "UnreadableState", "selector"]
