"""Failure diagnosis (PHASE2 T4, R1 and R3, D31).

A failed transaction gets one finding: the most specific rule that matches. Each rule says what
triggers it, which state read can confirm it, and how sure the result is:
  confirmed      a state read at the parent block proves the condition the reason names
  single_source  the explorer's reason is a known message with one meaning (its source is cited)
  candidate      words or a pattern that suggest a cause without proving it
Every finding comes with next steps that cannot make things worse when followed. Reads that were
made are stated as their own facts, so the finding can cite them. Nothing here guesses a token,
holder or amount the call data does not give.
"""
import re
from dataclasses import dataclass, field

from anychain.collectors.http import CollectorError
from anychain.collectors.types import RevertReason
from anychain.reads import Read, StateReader, UnreadableState

# A sub-call that runs out of gas leaves its caller 1/64 of the gas (EIP-150), so the caller can
# revert having used about 98.4 % of the limit.
ALL_GAS = 0.98


@dataclass
class Context:
    reason: RevertReason | None
    result: str | None  # the explorer's `result` text
    call: dict | None  # data of the decoded top-level call fact: {"function": ..., "args": {...}}
    sender: str | None
    to: str | None
    to_text: str  # how the answer names the target
    block: int | None  # block of the transaction
    gas_used: int | None
    gas_limit: int | None
    reader: StateReader | None  # None when the node cannot be used
    generic_failure: str | None = None  # the network type's note when `result` is a text that carries no reason
    explorer_text: str | None = None  # the explorer's failure text when it says more than "Reverted"
    block_time: int | None = None  # the block's timestamp (seconds since 1970-01-01 UTC)


@dataclass
class Missing:
    """A read the finding wanted but could not make, and what would fix it."""

    why: str
    needed: str
    cause: str  # a models.GapCause
    retryable: bool = False


@dataclass
class Finding:
    rule: str
    level: str  # confirmed | single_source | candidate
    text: str
    next_steps: list[str]
    reads: list[Read] = field(default_factory=list)
    missing: list[Missing] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)  # where the rule's meaning comes from
    data: dict = field(default_factory=dict)  # values the finding computed, kept in the fact's data
    label: str | None = None  # set by a branch whose own data contradicts or does not decode it (D42)


def reason_text(reason: RevertReason | None) -> str | None:
    """The explorer's reason as plain text: the Error(string) message, or the custom error's name."""
    if reason is None or not reason.reported or reason.carried_no_data:
        return None
    if reason.method_call and reason.method_call.startswith("Error(") and reason.parameters:
        return str(reason.parameters[0][1])
    if reason.method_call:
        return reason.method_call
    return str(reason.original) if not isinstance(reason.original, dict) else None


def diagnose(ctx: Context) -> Finding:
    for rule in RULES:
        finding = rule(ctx)
        if finding:
            return _with_passed_deadline(finding, ctx)
    raise AssertionError("the last rule always matches")


# ---- rules, most specific first -------------------------------------------------------------

def _generic_failure(ctx: Context) -> Finding | None:
    """The network type knows the explorer's failure text says nothing (e.g. zkSync's, ChainProfile)."""
    if not ctx.generic_failure:
        return None
    return Finding("generic_failure", "single_source", ctx.generic_failure,
                   ["Find the reason with a node that can trace the transaction, or ask the contract's developers."])


def _balance(ctx: Context) -> Finding | None:
    text = reason_text(ctx.reason) or ""
    if "transfer amount exceeds balance" not in text and not text.startswith("ERC20InsufficientBalance("):
        return None
    steps = ["Check the balance of the account the tokens are moved from before sending, and move at most that.",
             "If the balance was expected, check for an earlier transaction that spent it."]
    token_call = _token_call(ctx, holder_from_args=False)
    if token_call is None:
        return Finding("insufficient_balance", "single_source",
                       f"The reason is the standard ERC-20 message {text!r}: a token transfer inside this transaction "
                       "asked for more than the account the tokens were moved from held (in a swap or router call, "
                       "that can be a pool or a contract, not the sender). Which token and account cannot be read "
                       "from this call's data.", steps)
    token, holder, amount = token_call
    return _compare_read(ctx, "insufficient_balance", text, steps, amount,
                         lambda r, b: r.erc20_balance(token, holder, b), "balance", holder, token)


def _allowance(ctx: Context) -> Finding | None:
    text = reason_text(ctx.reason) or ""
    if "exceeds allowance" not in text and "insufficient allowance" not in text.lower() \
            and not text.startswith("ERC20InsufficientAllowance("):
        return None
    steps = ["Approve the spender for at least the amount (an approve transaction from the token holder), then "
             "send again."]
    token_call = _token_call(ctx, holder_from_args=True)
    if token_call is None or ctx.sender is None:
        return Finding("insufficient_allowance", "single_source",
                       f"The reason is the standard ERC-20 message {text!r}: a contract tried to move tokens "
                       "on someone's behalf beyond what they had approved. Which token and approval cannot "
                       "be read from this call's data.", steps)
    token, owner, amount = token_call  # direct transferFrom: the spender is whoever called it
    return _compare_read(ctx, "insufficient_allowance", text, steps, amount,
                         lambda r, b: r.erc20_allowance(token, owner, ctx.sender, b), "allowance", owner, token)


# OpenZeppelin's access checks, read in their source on 2026-10-08: v4.9.6 (commit dc44c9f) Ownable.sol L51
# "Ownable: caller is not the owner", AccessControl.sol L113-115 "AccessControl: account <0x…40> is missing role
# <0x…64>"; v5.0.2 (commit dbb6104) Ownable.sol L26 error OwnableUnauthorizedAccount, raised at L65 with _msgSender(), and
# Ownable2Step.sol L55 by acceptOwnership for a caller that is not the pending owner; AccessControl.sol L96
# AccessControlUnauthorizedAccount(account, role). The v5 errors and the v4 role message name the refused account.
OZ_V4 = "https://github.com/OpenZeppelin/openzeppelin-contracts/blob/dc44c9f1a4c3b10af99492eed84f83ed244203f6/contracts/access"
OZ_V5 = "https://github.com/OpenZeppelin/openzeppelin-contracts/blob/dbb6104ce834628e473d2173bbc9d47f81a9eec3/contracts/access"
ROLE_MESSAGE = re.compile(r"^AccessControl: account (0x[0-9a-fA-F]{40}) is missing role (0x[0-9a-fA-F]{64})$")
ACCESS_STEPS = ["The refusal comes from a permission check: sending the same call again unchanged fails the same way "
                "and costs the fee again.",
                "Find which account the check refused (OpenZeppelin v5 errors name it) and whether it should hold "
                "the permission; if it should, ask the contract's operators to grant it."]


def _access(ctx: Context) -> Finding | None:
    """An access-control refusal (OpenZeppelin Ownable or AccessControl), confirmed by reading the permission at
    the block before (PHASE2_5 T4, R5, D41). Confirmed only when the error names the refused account and it is the
    sender: then the check ran where msg.sender was the sender. The bare v4 owner text names no account, so the
    read on the contract called is stated, never taken as proof."""
    text = reason_text(ctx.reason) or ""
    call = (ctx.reason.method_call or "") if ctx.reason else ""
    params = dict(ctx.reason.parameters) if ctx.reason else {}
    role_text = ROLE_MESSAGE.match(text)
    if text == "Ownable: caller is not the owner":
        kind, account, role, named, url = "owner", None, None, False, f"{OZ_V4}/Ownable.sol#L51"
    elif role_text:
        kind, account, role, named, url = "role", role_text.group(1), role_text.group(2), True, f"{OZ_V4}/AccessControl.sol#L105-L119"
    elif call.startswith("OwnableUnauthorizedAccount("):
        kind, account, role, named, url = "owner", params.get("account"), None, True, f"{OZ_V5}/Ownable.sol#L26"
    elif call.startswith("AccessControlUnauthorizedAccount("):
        kind, account, role, named, url = "role", params.get("account"), params.get("neededRole"), True, f"{OZ_V5}/AccessControl.sol#L96"
    else:
        return None
    pending = kind == "owner" and named and (ctx.call or {}).get("function") == "acceptOwnership"
    what = ("the pending owner (Ownable2Step's acceptOwnership refuses with the same error)" if pending else
            "the owner" if kind == "owner" else f"an account with the role {role}" if role else "an account with the role")
    shown = text or call
    said = (f"The reason {shown!r} is OpenZeppelin's access check: a call was refused because the caller of that check "
            f"(the sender or a contract in between) is not {what}.")

    def stated(extra: str = "", missing=None, reads=None, label=None) -> Finding:
        return Finding("access_control", "single_source", said + extra, ACCESS_STEPS, list(reads or []),
                       missing=list(missing or []), source_urls=[url], label=label)
    if named and not account:
        return stated(missing=[Missing("The refused account was not read: the explorer did not give the error's "
                                       "account parameter", "The error's parameters decoded with its ABI",
                                       "not_interpretable")])
    if account and ctx.sender and str(account).lower() != ctx.sender.lower():
        return stated(f" The refused account is {account}, not the sender {ctx.sender}: the check ran on a call made by "
                      "a contract in between, so no read of the contract called proves it.")
    if kind == "role" and not (isinstance(role, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", role)):
        return stated(missing=[Missing(f"hasRole was not read: the role the error gives ({role!r}) is not 32 bytes of hex",
                                       "The error's neededRole decoded with its ABI", "not_interpretable")])
    signature = "pendingOwner()" if pending else "owner()" if kind == "owner" else "hasRole(bytes32,address)"
    if ctx.reader is None or ctx.to is None or ctx.block is None or not ctx.sender:
        return stated(missing=[_unavailable(signature, ctx)])
    try:
        read = (ctx.reader.pending_owner(ctx.to, ctx.block - 1) if pending else
                ctx.reader.owner(ctx.to, ctx.block - 1) if kind == "owner" else
                ctx.reader.has_role(ctx.to, role, ctx.sender, ctx.block - 1))
    except (CollectorError, UnreadableState) as exc:
        return stated(missing=[_failed_read(signature, exc)])
    if kind == "owner" and not isinstance(read.value, str):
        return stated(missing=[Missing(f"{signature} on {ctx.to} answered {read.value!r}, not an address",
                                       "Nothing: the contract does not answer this standard read", "not_interpretable")],
                      reads=[read])
    lacks = (read.value.lower() != ctx.sender.lower()) if kind == "owner" else read.value is False
    if lacks and not named:
        return stated(f" At block {read.block}, {signature} of {ctx.to_text} returned {read.value}, not the sender; but "
                      "this text names no account, so the check may have run in a contract further down the call, "
                      "where the caller was not the sender.", reads=[read])
    if lacks:
        what_read = (f"{signature} of {ctx.to_text} returned {read.value}, not the sender {ctx.sender}" if kind == "owner"
                     else f"hasRole({role}, {ctx.sender}) on {ctx.to_text} returned false")
        return Finding("access_control", "confirmed",
                       f"Cause confirmed: the error names the sender as the refused account, and at block {read.block}, "
                       f"the block before, {what_read}, matching the reason {shown!r}"
                       + (" (the caller is not the pending owner)." if pending else "."),
                       ACCESS_STEPS, [read], source_urls=[url])
    return stated(f" But at block {read.block} the sender held that permission on the contract it called "
                  f"({signature} answered {read.value}): the check that failed is in another contract further down "
                  "the call, or the permission changed earlier in this transaction's own block.", reads=[read],
                  label="LIKELY")


def _paused(ctx: Context) -> Finding | None:
    text = reason_text(ctx.reason) or ""
    if text.strip().lower() not in ("paused", "pausable: paused") and not text.startswith("EnforcedPause("):
        return None
    steps = ["Wait until the contract's operator unpauses it; calls to it fail while it is paused.",
             "Check the project's announcements for why it was paused."]
    said = f"The transaction reverted with {text!r}, the reason a paused contract gives."
    if ctx.reader is None or ctx.to is None or ctx.block is None:
        return Finding("paused", "single_source", said, steps,
                       missing=[_unavailable("paused()", ctx)])
    try:
        read = ctx.reader.paused(ctx.to, ctx.block - 1)
    except (CollectorError, UnreadableState) as exc:
        return Finding("paused", "single_source", said, steps, missing=[_failed_read("paused()", exc)])
    if read.value is True:
        return Finding("paused", "confirmed",
                       f"Cause confirmed: the contract this transaction called, {ctx.to_text}, was paused at block "
                       f"{read.block}, the block before it (paused() returned true), matching the reason {text!r}.",
                       steps, [read])
    return Finding("paused", "single_source",
                   f"{said} But the contract this transaction called was not paused at block {read.block} (paused() "
                   "returned false): another contract further down the call may be the paused one, or it was paused "
                   "earlier in this transaction's own block.", steps, [read], label="LIKELY")


# Uniswap routers' checks, read in their source on 2026-10-08:
#   v2-periphery UniswapV2Router02.sol (commit ed24991): L19 "UniswapV2Router: EXPIRED", L232
#   "UniswapV2Router: INSUFFICIENT_OUTPUT_AMOUNT", L246 "UniswapV2Router: EXCESSIVE_INPUT_AMOUNT";
#   v3-periphery PeripheryValidation.sol (commit 6a31c61) L8 "Transaction too old"; SwapRouter.sol
#   (commit 0682387) and swap-router-contracts V3SwapRouter.sol (SwapRouter02, commit 70bc2e4) L128
#   "Too little received", L218/L220 "Too much requested". Other contracts reuse the words: a text is
#   stated as the router's check only with the v2 prefix, or when the decoded call is one of the routers'
#   swap functions; otherwise it is a candidate.
V2_ROUTER = "https://github.com/Uniswap/v2-periphery/blob/ed24991304291297c3b4a52818d02f46a17aa9a2/contracts/UniswapV2Router02.sol"
V3_VALIDATION = ("https://github.com/Uniswap/v3-periphery/blob/6a31c618fc3180a6ee945b869d1ce4449f253ee6/"
                 "contracts/base/PeripheryValidation.sol#L8")
V3_ROUTER = "https://github.com/Uniswap/v3-periphery/blob/0682387198a24c7cd63566a2c58398533860a5d1/contracts/SwapRouter.sol"
V3_ROUTER_02 = ("https://github.com/Uniswap/swap-router-contracts/blob/70bc2e40dfca294c1cea9bf67a4036732ee54303/"
                "contracts/V3SwapRouter.sol")
SLIPPAGE = {
    "UniswapV2Router: INSUFFICIENT_OUTPUT_AMOUNT": ("less than the minimum output", [f"{V2_ROUTER}#L232"]),
    "UniswapV2Router: EXCESSIVE_INPUT_AMOUNT": ("more than the maximum input", [f"{V2_ROUTER}#L246"]),
    "Too little received": ("less than the minimum output", [f"{V3_ROUTER}#L128", f"{V3_ROUTER_02}#L128"]),
    "Too much requested": ("more than the maximum input", [f"{V3_ROUTER}#L218", f"{V3_ROUTER_02}#L220"]),
}
DEADLINES = {"Transaction too old": [V3_VALIDATION], "UniswapV2Router: EXPIRED": [f"{V2_ROUTER}#L19"]}
# The routers' swap entry points (v2: swapExact*, swapTokensFor*, swapETHFor*; v3: exactInput*, exactOutput*)
SWAP_FUNCTIONS = ("swapExact", "swapTokensFor", "swapETHFor", "exactInput", "exactOutput")
SLIPPAGE_STEPS = ["Check the current price before sending again. A wider slippage tolerance lets the swap go through "
                  "at a worse price and exposes it to front-running (sandwich) losses; prefer waiting for the price "
                  "to settle or a smaller trade.", "For large swaps, split them or use a deeper pool."]


def _slippage(ctx: Context) -> Finding | None:
    text = reason_text(ctx.reason) or ""
    function = (ctx.call or {}).get("function") or ""
    if text in SLIPPAGE and (text.startswith("UniswapV2Router:") or function.startswith(SWAP_FUNCTIONS)):
        meaning, urls = SLIPPAGE[text]
        return Finding("slippage", "single_source",
                       f"The reason {text!r} is the slippage check of Uniswap's routers: when the transaction ran, the "
                       f"swap would have needed {meaning} the sender had set, so it was refused. The price moved, or "
                       "the limit was too tight.", SLIPPAGE_STEPS, source_urls=urls)
    reused = next((t for t in SLIPPAGE if t.split(": ")[-1].lower() == text.lower()), None)
    if reused:
        meaning, urls = SLIPPAGE[reused]
        return Finding("slippage", "candidate",
                       f"Possible cause: the reason {text!r} uses the words of the slippage check of Uniswap's routers "
                       f"(the swap would have given {meaning} the sender set). This contract may use them with the "
                       "same meaning; its own code decides.", SLIPPAGE_STEPS, source_urls=urls)
    return None


DEADLINE_ARGS = ("deadline", "_deadline", "deadline_")
DEADLINE_STEP = ("Check the current price, then send it again with a new deadline (and a fee high enough to be "
                 "included in time).")


def _deadline(ctx: Context) -> Finding | None:
    """A deadline reason, with the call's own deadline parameter compared with the block's time when it has one
    (PHASE2_5 T4, R6, D41). With no reason at all, the comparison is added to the finding that explains the
    missing reason instead (`diagnose`), so the out-of-gas candidate and the replay still run."""
    text = reason_text(ctx.reason) or ""
    timed_text = any(w in text.lower() for w in ("deadline", "expired", "too old"))
    if text not in DEADLINES and not timed_text:
        return None
    compared = _deadline_compared(ctx)
    data = {"deadline_check": compared} if compared else {}
    if text in DEADLINES:
        if compared and compared["deadline_passed"]:
            return Finding("deadline", "single_source",
                           f"{compared['text']}: the deadline had passed when the transaction was included, and the "
                           f"reason {text!r} is Uniswap's deadline check.", [DEADLINE_STEP],
                           source_urls=DEADLINES[text], data=data)
        if compared:
            return Finding("deadline", "single_source",
                           f"The reason {text!r} is Uniswap's deadline check. {compared['text']}, so the deadline "
                           "is not before the block's time: the deadline that was checked is not this call's "
                           "deadline parameter.", [DEADLINE_STEP], source_urls=DEADLINES[text], data=data,
                           label="LIKELY")
        return Finding("deadline", "single_source",
                       f"The reason {text!r} is Uniswap's deadline check: the transaction was included after the "
                       "deadline the sender set in it.", [DEADLINE_STEP], source_urls=DEADLINES[text])
    after = ""
    if compared:
        after = (f" {compared['text']}: this call's deadline had passed when the transaction was included."
                 if compared["deadline_passed"] else
                 f" {compared['text']}, so this call's deadline is not before the block's time.")
    return Finding("deadline", "candidate",
                   f"Possible cause: the reason {text!r} mentions a time limit. Which limit (one set in the "
                   "transaction, a signature's, an order's, a price's freshness) and in which direction is "
                   f"defined in the contract's code.{after}",
                   ["Read the contract's code for this reason before sending again: resending may fail the "
                    "same way and cost the fee again."], data=data)


def _deadline_compared(ctx: Context) -> dict | None:
    """The call's deadline parameter against the block's time, when the decoded call has one."""
    args = (ctx.call or {}).get("args") or {}
    value = next((args[k] for k in DEADLINE_ARGS if k in args), None)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or ctx.block_time is None:
        return None
    return {"deadline": value, "block_time": ctx.block_time, "deadline_passed": value < ctx.block_time,
            "text": f"The call's deadline parameter is {value} ({_utc(value)}) and the block's time is "
                    f"{ctx.block_time} ({_utc(ctx.block_time)})"}


NO_REASON_RULES = ("no_reason", "possibly_out_of_gas")


def _with_passed_deadline(finding: Finding, ctx: Context) -> Finding:
    """A finding for a failure with no reason, plus the call's deadline compared with the block's time when the
    deadline had passed: a possible cause next to the others, never in their place."""
    compared = _deadline_compared(ctx)
    if finding.rule not in NO_REASON_RULES or not compared or not compared["deadline_passed"]:
        return finding
    finding.text += (f" Also: {compared['text'][0].lower()}{compared['text'][1:]}: this call's deadline had passed when "
                     "it was included, and a contract that takes a deadline usually rejects such a call; with no "
                     "reason, this is a possibility, not a confirmed cause.")
    finding.data["deadline_check"] = compared
    return finding


def _utc(seconds: int) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return "beyond any calendar date"


def _all_gas_no_reason(ctx: Context) -> Finding | None:
    if ctx.reason is None or not ctx.reason.carried_no_data or not ctx.gas_used or not ctx.gas_limit:
        return None
    if ctx.gas_used / ctx.gas_limit < ALL_GAS:
        return None
    return Finding("possibly_out_of_gas", "candidate",
                   f"Possible cause: the transaction used {ctx.gas_used} of its {ctx.gas_limit} gas limit and reverted "
                   "with no reason, which is what running out of gas inside a call looks like. A failed assert in "
                   "old Solidity or an invalid instruction also uses all the gas. The explorer does not say which.",
                   ["Estimate the gas for the same call first. If the estimate fails, the call fails for another "
                    "reason: do not send it again with more gas. If it succeeds, send with that gas limit."])


def _contract_reason(ctx: Context) -> Finding | None:
    text = reason_text(ctx.reason)
    if not text and not (ctx.reason is not None and ctx.reason.carried_no_data):
        text = ctx.explorer_text
    if not text or text.lower() == "execution reverted":  # the node's words for "reverted", not a reason
        return None
    decoded = bool(reason_text(ctx.reason))
    return Finding("contract_reason", "single_source",
                   (f"The contract reverted with its own reason {text!r}. What it means is defined in the "
                    "contract's code." if decoded else
                    f"The explorer reports the failure as {text!r}; no decoded reason confirms it."),
                   ["Read the contract's source where this reason is raised, or ask its developers.",
                    "Do not send the same transaction again until the condition behind the reason has changed."],
                   label=None if decoded else "LIKELY")


NO_REASON_STEPS = ["Find where it reverted with a node that can trace the transaction, or ask the contract's "
                   "developers."]


def _no_reason(ctx: Context) -> Finding | None:
    return Finding("no_reason", "single_source", "No reason is available for this failure.", list(NO_REASON_STEPS))


# Next steps for a non-technical reader (a merchant using an app), one set per rule; the rules' own steps are
# the developer's (PHASE2_5 T5, R7, D42). Actions only: what happened is said by the facts, not here.
SUPPORT_STEPS = {
    "generic_failure": ["Contact the support of the app or service you used, with the link to this transaction, "
                        "before trying again."],
    "insufficient_balance": ["The operation tried to move more of a token than the account it comes from held. "
                             "Check the amount and that balance before trying again."],
    "insufficient_allowance": ["The operation needed permission to move a token (an approval) that was missing or "
                               "too small. Check in the app that its approval step was completed before trying again."],
    "access_control": ["The contract refused the operation for lack of permission. Contact the app's or project's "
                       "support with the link to this transaction: sending it again the same way fails and charges "
                       "the fee again."],
    "paused": ["The contract refused the operation with a message about being paused. Check the project's "
               "announcements, and contact its support before trying again."],
    "slippage": ["The swap was refused by its price limit. Check the current price, and try again with a smaller "
                 "amount or a limit that fits it, or contact the app's support."],
    "deadline": ["The operation was refused by a time-limit check. Contact the app's support with the link to this "
                 "transaction before trying again."],
    "possibly_out_of_gas": ["Contact the app's support with the link to this transaction before trying again: "
                            "trying again may fail the same way and charge the fee again."],
    "contract_reason": ["The service refused the operation with its own message. Contact the app's or project's "
                        "support with that message and the link to this transaction."],
    "no_reason": ["Contact the app's support with the link to this transaction before trying again, so the fee is "
                  "not charged again for the same failure."],
    "replay": ["Contact the app's support with the link to this transaction before trying again."],
}
# Rules whose conclusion is the explorer's decoded reason itself (the original plan: "provado por revert reason
# decodificado"); a read upgrades any rule to confirmed.
REASON_RULES = {"insufficient_balance", "insufficient_allowance", "access_control", "paused", "slippage", "deadline",
                "contract_reason"}


def label_for(rule: str, level: str, label: str | None = None) -> str:
    """The original plan's label for a conclusion (PHASE2_5 D4, D42): CONFIRMED, proven by a decoded revert reason or
    a read; LIKELY, suggested by a pattern or a replay, or a reason the finding's own read contradicts or that was not
    decoded (`label`, set by the branch); UNKNOWN, nothing to infer from (the gaps say what is missing)."""
    if label:
        return label
    if level == "confirmed":
        return "CONFIRMED"
    if level == "candidate":
        return "LIKELY"
    return "CONFIRMED" if rule in REASON_RULES else "UNKNOWN"


def steps_text(rule: str, developer: list[str]) -> str:
    return (" ".join(f"Next step for a non-technical reader: {s}" for s in SUPPORT_STEPS[rule]) + " "
            + " ".join(f"Next step for a developer: {s}" for s in developer))


RULES = [_generic_failure, _balance, _allowance, _access, _paused, _slippage, _deadline, _all_gas_no_reason,
         _contract_reason,
         _no_reason]


# ---- helpers ----------------------------------------------------------------------------------

def _token_call(ctx: Context, holder_from_args: bool) -> tuple[str, str, int] | None:
    """(token, holder or owner, amount) when the top-level call is transfer/transferFrom on a token."""
    if not ctx.call or not ctx.to or not ctx.sender:
        return None
    args = list((ctx.call.get("args") or {}).values())
    function = ctx.call.get("function")
    if function == "transfer" and len(args) == 2 and not holder_from_args:
        return ctx.to, ctx.sender, _int(args[1])
    if function == "transferFrom" and len(args) == 3:
        return ctx.to, str(args[0]), _int(args[2])
    return None


def _int(value: object) -> int | None:
    try:
        return int(value)  # decoded uint256 arguments are ints, or their decimal text
    except (TypeError, ValueError):
        return None


def _unavailable(what: str, ctx: Context) -> Missing:
    if ctx.reader is None:
        return Missing(f"{what} was not read: the network's node is not available or its chain was not confirmed",
                       "The configured RPC node reachable and on the configured chain", "source_unavailable", True)
    return Missing(f"{what} was not read: the transaction's target or block is not known",
                   "The transaction's target and block from the explorer or the node", "not_interpretable")


def _failed_read(what: str, exc: Exception) -> Missing:
    if isinstance(exc, UnreadableState):
        return Missing(f"{what} could not be read: {exc}", "Nothing: the contract does not answer this standard read",
                       "not_interpretable")
    if isinstance(exc, CollectorError) and exc.retryable:
        return Missing(f"{what} could not be read: {exc}", "Try again in a few minutes", "source_unavailable", True)
    return Missing(f"{what} could not be read: {exc}", "A node that keeps state for the block before the transaction",
                   "source_error")


def _compare_read(ctx: Context, rule: str, text: str, steps: list[str], amount: int | None, read_fn, what: str,
                  holder: str, token: str) -> Finding:
    base = f"The reason is the standard ERC-20 message {text!r}."
    if amount is None:
        return Finding(rule, "single_source", base, steps,
                       missing=[Missing(f"the {what} was not compared: the amount in the call data is not readable",
                                        "A decodable call", "not_interpretable")])
    if ctx.reader is None or ctx.block is None:
        return Finding(rule, "single_source", base, steps, missing=[_unavailable(f"the {what}", ctx)])
    try:
        read = read_fn(ctx.reader, ctx.block - 1)
    except (CollectorError, UnreadableState) as exc:
        return Finding(rule, "single_source", base, steps, missing=[_failed_read(f"the {what}", exc)])
    if read.value < amount:
        return Finding(rule, "confirmed",
                       f"Cause confirmed: at block {read.block}, the block before this transaction, the {what} of "
                       f"{holder} for token {token} was {read.value} (raw units), less than the {amount} the call "
                       f"asked for. {base}", steps, [read])
    return Finding(rule, "single_source",
                   f"{base} The read does not explain it: at block {read.block} the {what} of {holder} for token "
                   f"{token} was {read.value} (raw units), enough for the {amount} asked. Possible reasons: it changed "
                   "earlier in this transaction's own block; the token checks more than this value (locked or frozen "
                   "amounts, a transfer fee); or another transfer inside the call is the one that failed.",
                   steps, [read], label="LIKELY")
