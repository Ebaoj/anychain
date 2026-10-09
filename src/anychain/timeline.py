"""Multi-transaction timeline (PHASE4 T2, R2, the plan's bonus 3): for a failed transaction, the sender's own
transactions just before and after it, and the patterns the plan names, said only when the data shows them:
  retried_ok         the same call (same method, same contract) succeeded later
  approved_then_ok   an approve came after the failure, then the same call succeeded
  repeated_failures  the same call failed several times in a row

Pure functions over the explorer's rows; the bundle fetches them (bounded) and adds the facts.
"""
from dataclasses import dataclass
from datetime import datetime

from anychain.collectors.types import AddressTransaction

AROUND = 3  # transactions shown on each side of the failure


@dataclass
class Timeline:
    rows: list[AddressTransaction]  # the failure and its neighbours, by nonce
    failed: AddressTransaction
    patterns: list[tuple[str, str]]  # (name, sentence)


def build(failed_hash: str, sender: str, rows: list[AddressTransaction]) -> Timeline | None:
    sent = {r.hash.lower(): r for r in rows if r.hash and (r.sender or "").lower() == sender.lower()
            and r.nonce is not None}
    failed = sent.get(failed_hash.lower())
    if failed is None:
        return None
    # Only the unbroken run of nonces around the failure: a page not read leaves a hole, and nothing is said across
    # it (real: Celo nonces 68278 then 68569 looked "in a row" before this rule).
    by_nonce = {r.nonce: r for r in sent.values()}
    low = high = failed.nonce
    while low - 1 in by_nonce:
        low -= 1
    while high + 1 in by_nonce:
        high += 1
    ordered = [by_nonce[n] for n in range(low, high + 1)]
    at = ordered.index(failed)
    window = ordered[max(0, at - AROUND): at + AROUND + 1]
    return Timeline(window, failed, _patterns(failed, ordered[:at], ordered[at + 1:]))


def _same_call(a: AddressTransaction, b: AddressTransaction) -> bool:
    return bool(a.method) and a.method == b.method and (a.to or "").lower() == (b.to or "").lower()


def _ok(r: AddressTransaction) -> bool:
    return r.result == "success"


def _patterns(failed, before, after) -> list[tuple[str, str]]:
    out = []
    later_ok = next((r for r in after if _same_call(r, failed) and _ok(r)), None)
    if later_ok is not None:
        between = after[:after.index(later_ok)]
        approve = next((r for r in between if (r.method or "").lower() == "approve" and _ok(r)), None)
        when = _later(_seconds(failed.timestamp, later_ok.timestamp))
        if approve is not None:
            out.append(("approved_then_ok", f"After the failure the sender sent an approve (nonce {approve.nonce}), "
                        f"then the same call ({failed.method}) succeeded {when} (nonce {later_ok.nonce})."))
        else:
            out.append(("retried_ok", f"The same call ({failed.method} to {failed.to}) succeeded {when} (nonce "
                        f"{later_ok.nonce}): the first time it succeeded after this failure."))
    run = [failed]
    for side in (list(reversed(before)), after):
        for r in side:
            if _same_call(r, failed) and not _ok(r):
                run.append(r)
            else:
                break
    if len(run) >= 2:
        nonces = sorted(r.nonce for r in run)
        listed = (", ".join(str(n) for n in nonces) if len(nonces) <= 6 else f"nonces {nonces[0]} to {nonces[-1]}")
        out.append(("repeated_failures", f"The same call ({failed.method}) failed {len(run)} times in a row, with "
                    f"no other transaction of the sender in between ({'nonces ' if len(nonces) <= 6 else ''}"
                    f"{listed})."))
    return out


def _later(seconds: int | None) -> str:
    """"N seconds later", with days or hours added for long gaps (the number of seconds stays: it is the fact)."""
    if seconds is None:
        return "later"
    for unit, size in (("day", 86400), ("hour", 3600)):
        if seconds >= size:
            n = round(seconds / size)
            return f"{seconds} seconds later (about {n} {unit}{'' if n == 1 else 's'})"
    return f"{seconds} seconds later"


def _seconds(a: str | None, b: str | None) -> int | None:
    try:
        return int((_time(b) - _time(a)).total_seconds())
    except (TypeError, ValueError):
        return None


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
