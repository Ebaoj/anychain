"""Gas notes (PHASE4 T3, R3, the plan's bonus 5, small scope). Always notes, never findings:
  - how much of the gas limit was used;
  - the same call's gas in the sender's own successful attempt, when the timeline (D56) has one;
  - a heuristic on the called function's verified code: a mutable storage variable used inside a loop.
"""
import re

from anychain.security import LOCAL_DECLARATION, NOT_TYPES, _parameters, _parse, _state_variables
from anychain.solidity import mask_strings

MAX_LOOP_NOTES = 3


def usage_note(used: int | None, limit: int | None, status: str) -> str | None:
    if not used or not limit:
        return None
    pct = round(100 * used / limit, 1)
    text = f"Gas used {used} of the {limit} limit ({pct}%)."
    if status == "success" and pct >= 95:
        text += " It succeeded close to its limit: the same call doing a little more work could run out of gas."
    elif status == "success" and pct < 50:
        text += " The limit was more than twice the gas used."
    return text


def same_call_note(timeline_fact) -> str | None:
    """From the timeline fact's rows: the same call (method and contract) that succeeded, with its gas."""
    rows = timeline_fact.data.get("rows") or []
    failed = next((r for r in rows if r.get("this")), None)
    if failed is None:
        return None
    ok = [r for r in rows if r is not failed and r.get("result") == "success" and r.get("method")
          and r.get("method") == failed.get("method") and (r.get("to") or "").lower() == (failed.get("to") or "").lower()
          and r.get("gas_used") and r.get("gas_limit") and failed.get("gas_limit")]
    if not ok:
        return None
    later = [r for r in ok if (r.get("nonce") or 0) > (failed.get("nonce") or 0)]
    r = (later or ok)[0]  # the attempt after the failure first, as the timeline's pattern names it
    return (f"The same call succeeded at nonce {r['nonce']} using {r['gas_used']} gas, with a limit of "
            f"{r['gas_limit']} ({timeline_fact.id}); this transaction had a limit of {failed.get('gas_limit')}.")


def storage_in_loop(index, found, contract: str) -> list[tuple[int, str]]:
    """Lines inside a loop of the called function that use a mutable storage variable (heuristic)."""
    f, c = found.function, found.contract
    code = mask_strings(index.uncommented.get(c.path, "")).split("\n")
    header, statements = _parse(list(zip(range(f.start, f.end + 1), code[f.start - 1:f.end])))
    declared = _parameters(header, f.name) | {m.group(2) for s in statements for m in LOCAL_DECLARATION.finditer(s.text)
                                               if m.group(1) not in NOT_TYPES}
    state = sorted(_state_variables(index, contract) - declared)
    notes, seen = [], set()
    for s in statements:
        if not any(s.kinds.get(b) == "loop" for b in s.blocks):
            continue
        for var in state:
            # a plain read only: not indexed (`v[i]`: a different slot each time), not a member call (`v.set(..)`),
            # not assigned (`v = ..`, `v += ..`); real: ValidatorTimelock's `committedBatchTimestamp[..].set(i, ..)`
            # writes a different slot per iteration, where "read it once before the loop" would be wrong
            m = re.search(rf"(?<![\w.]){re.escape(var)}\b(?!\s*(?:\[|\.|\(|[-+*/%|&^]?=(?!=)|\+\+|--))", s.text)
            if m and var not in seen:
                seen.add(var)
                n = s.line_at(m.start())
                notes.append((n, f"line {n} uses the storage variable {var} inside a loop: if its value does not "
                                 "change in the loop, reading it once before the loop saves a storage read per "
                                 "iteration"))
    return notes[:MAX_LOOP_NOTES]
