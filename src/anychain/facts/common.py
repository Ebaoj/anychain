"""What every part of the evidence builder shares: imports, small helpers and constants (split out of bundle.py,
D79)."""
import re

import httpx
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from eth_utils import to_checksum_address

from anychain.chains import BLOCKSCOUT_CHAIN_TYPES, ChainGap, Fee, FeePart, TokenRef, audit_fields, profile_for
from anychain.collectors.explorer import MAX_PAGES, ExplorerClient
from anychain.collectors.http import Budget, CollectorError, NotFoundError
from anychain.collectors.rpc import InsufficientFunds, RpcClient
from anychain.collectors.types import (
    AddressRef, Authorization, InternalCall, Log, RevertReason, RpcReceipt, RpcTransaction, TokenTransfer, Transaction,
    to_int,
)
from anychain.config import AppConfig
from eth_utils import keccak

from anychain.collectors.signatures import SignatureDb
from anychain.decoder import AbiDecoder, decode_revert, fit_signature
from anychain.collectors.repo import Repo, RepoCache
from anychain.diagnosis import SUPPORT_STEPS, Context, diagnose, label_for, reason_text, steps_text
from anychain import gas, security, timeline
from anychain.solidity import SolidityIndex, filter_abi
from anychain.reads import REPLAY_LIMITS, StateReader
from anychain.models import EvidenceBundle, GapCause, Source

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_EVENTS = 40
REPLAY_WHEN = {"no_reason", "generic_failure", "possibly_out_of_gas"}  # findings without the explorer's reason
MAX_EVENT_LOOKUPS = 10  # distinct undecoded event topics looked up per answer
MAX_CODE_LINES = 80  # lines of one function given as a code fact; the rest is counted (D38)
MAX_CODE_CHARS = 200  # characters of one code line given to the model


def render_code(lines: list[str], start: int, raise_line: int | None = None, show: tuple[int, ...] = ()) -> str:
    """Numbered source lines; a long function keeps the lines around `raise_line` and around each line in
    `show` (lines a fact cites), then its first lines, saying how many are not shown (reviews of D38 and
    D39: a cited line is always shown)."""
    n = len(lines)

    def around(line: int, half: int) -> set[int]:
        r = line - start
        return set(range(max(0, r - half), min(n, r + half + 1))) if 0 <= r < n else set()
    if n <= MAX_CODE_LINES:
        keep = set(range(n))
    else:
        must = ({raise_line - start} if raise_line is not None and 0 <= raise_line - start < n else set()) | \
               {s - start for s in show if 0 <= s - start < n}
        keep = (around(raise_line, MAX_CODE_LINES // 4) if raise_line is not None else set()) | \
            {i for s in show for i in around(s, 2)}
        if len(keep) > MAX_CODE_LINES:
            keep = set(must)
        i = 0
        while len(keep) < MAX_CODE_LINES and i < n:
            keep.add(i)
            i += 1
    out, last = [], -1
    for i in sorted(keep):
        if i != last + 1:
            out.append(f"({_lines(i - last - 1)} not shown)")
        text = lines[i] if len(lines[i]) <= MAX_CODE_CHARS else lines[i][:MAX_CODE_CHARS] + " …"
        out.append(f"{start + i} | {text}")
        last = i
    if last < n - 1:
        more = n - 1 - last
        out.append(f"({more} more {'line' if more == 1 else 'lines'} not shown)")
    return "\n".join(out)


def _lines(n: int) -> str:
    return f"{n} line" if n == 1 else f"{n} lines"


def _unix(timestamp: str | None) -> int | None:
    """An explorer timestamp ("2026-10-08T17:11:47.000000Z") as seconds since 1970-01-01 UTC."""
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()) if timestamp else None
    except ValueError:
        return None


def _raises_note(line: int | None, text: str | None) -> str:
    return (f"\nIn this function the reason {text!r} is written only at line {line}, so the failure was most "
            f"likely raised there (unless it was passed on from another function or contract this one called).")
MAX_PLACES = 3  # places a reason text is listed at, per source; the rest are counted
DEPOSIT_TX_TYPES = {0x7E, 0xFF}  # OP Stack deposits and zkSync L1->L2 priority txs: funds minted as they run
# What a replayed reason means, per diagnosis rule, worded about the replay (D32).
REPLAY_MEANING = {
    "insufficient_balance": "the standard ERC-20 message for a transfer larger than the balance it moves from",
    "insufficient_allowance": "the standard ERC-20 message for moving tokens beyond an approval",
    "paused": "the reason a paused contract gives",
    "access_control": "an access check (an owner or a role) refusing the caller",  # devnet, 2026-10-08 (D60)
    ("slippage", "single_source"): "the slippage check of Uniswap's routers (the swap would not meet the "
                                    "sender's limit)",
    ("slippage", "candidate"): "the words of the slippage check of Uniswap's routers; this contract's code decides "
                               "what they mean",
    ("deadline", "single_source"): "Uniswap's deadline check (included after the deadline set in it)",
    ("deadline", "candidate"): "a reason that mentions a time limit; which one is defined in the contract's code",
    "contract_reason": "the contract's own reason; what it means is defined in its code",
}
PREFETCH_WORKERS = 6  # parallel explorer requests while prefetching (D26)
MAX_INTERNAL = 30
TIMELINE_PAGES = 3  # pages of the sender's latest transactions read to reach a failure (PHASE4 R2)
ZERO_ADDRESS = "0x" + "0" * 40

# Internal call types whose `value` is only the caller's context, never a payment:
# delegatecall and callcode run another contract's code inside the caller, so any
# value stays with the caller; staticcall cannot carry value. (See DECISIONS D17.)
VALUE_IS_CONTEXT = {"delegatecall", "callcode", "staticcall"}

# Blockscout `status` -> our status. `status` is null while pending.
EXPLORER_STATUS = {"ok": "success", "error": "failed"}
# Blockscout `result` values that describe state, not a failure reason.
RESULT_WITHOUT_REASON = {
    "success", "pending", "awaiting_internal_transactions", "dropped/replaced", "Reverted", "error",
}


class InvalidHashError(ValueError):
    """The input is not a transaction hash."""


@dataclass(frozen=True)
class RpcView:
    """What the RPC node told us about the transaction."""

    tx: RpcTransaction
    receipt: RpcReceipt | None  # None while pending


# ---- small pure helpers ------------------------------------------------------

def amount(raw: int, decimals: int) -> str:
    """Exact decimal string for an integer amount (no float rounding)."""
    if decimals <= 0:
        return str(raw)
    whole, frac = divmod(raw, 10**decimals)
    if frac == 0:
        return str(whole)
    return f"{whole}.{str(frac).rjust(decimals, '0').rstrip('0')}"


def party(ref: AddressRef | str | None, label_for: Callable[[str], str | None] | None = None) -> str:
    """An address with its explorer name, or else the config's name for it (label_for)."""
    if ref is None or ref == "":
        return "(none)"
    if isinstance(ref, str):
        ref = AddressRef(ref)
    if ref.address is None:
        return "(unknown address)"
    name = ref.name or (label_for(ref.address) if label_for else None)
    return f"{ref.address} ({name})" if name else ref.address


def address_of(ref: AddressRef | None) -> str | None:
    return ref.address if ref else None


def with_args(args: list) -> str:
    """' with a=1, b=2' for decoded arguments, or '' when there are none."""
    return " with " + ", ".join(f"{a.name}={a.value}" for a in args) if args else ""


# ---- the builder -------------------------------------------------------------


class UnreadableLog(ValueError):
    """The explorer sent a log this tool cannot read (a source problem, not a defect here)."""


def _failure_note(it: InternalCall, short: bool = False) -> str:
    """Why an internal call has no effect, in the explorer's terms (values seen in real recordings)."""
    if it.error == "Parent reverted":  # the call itself may have succeeded; a caller above it reverted
        return "undone because a call above it reverted" if short else "but was undone because a call above it reverted"
    if it.error == "Reverted":
        return "this internal call reverted" if short else "but reverted"
    reason = f": the explorer reports {it.error!r}" if it.error else ""
    return f"this internal call failed{reason}" if short else f"but failed{reason}"


def _source_cause(retryable: bool) -> GapCause:
    """A source that failed: unavailable (worth retrying) or refused the request."""
    return "source_unavailable" if retryable else "source_error"
