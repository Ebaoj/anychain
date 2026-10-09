"""The outline of one answer, built by code from this transaction's facts (D62): which sections to write, in what
order, and which facts each must cite. A small model writes well from an outline and badly when it must decide what
matters: given a fixed list of sections it fills every one, with sentences no fact supports (measured: gpt-4.1-nano
with a fixed outline cited 41.9% of its sentences, against 72.6% without). Here a section exists only when a fact
is about it, and it names its facts."""
from anychain.models import EvidenceBundle

MOVEMENTS = ("token_transfer", "native_transfer")


def _ids(bundle: EvidenceBundle, *kinds: str, where=None) -> list[str]:
    return [e.id for e in bundle.items if e.kind in kinds and (where is None or where(e))]


def build(bundle: EvidenceBundle, mode: str) -> list[tuple[str, str, list[str]]]:
    """[(heading, what to say, fact ids)] for this transaction and reader."""
    overview = _ids(bundle, "overview")
    diagnosis = _ids(bundle, "diagnosis")
    revert = _ids(bundle, "revert")
    moves = _ids(bundle, *MOVEMENTS)
    fee = _ids(bundle, "fee")
    timeline = _ids(bundle, "timeline", where=lambda e: e.data.get("pattern"))
    triage = _ids(bundle, "triage")
    failed = bundle.status == "failed"
    out = []
    if mode == "support":
        out.append(("What happened", "did it work or fail, and why, in plain words; for a failure, each conclusion with "
                    "its label in the answer's language; then, in parentheses, its specifics for support: the "
                    "reason's original words, the error of the inner call that failed when the conclusion gives one (such as "
                    "'out of gas'), the figures, and the names of the contracts where it failed", overview + diagnosis))
        if triage:
            out.append(("What you told us", "what the reader said and what the facts show about it", triage))
        out.append(("Did the money move?", "yes or no, how much and from whom to whom" if moves else
                    ("no: it was not transferred because the transaction failed" if failed else
                     "say what the transaction moved, from the facts"), moves or overview))
        if fee:
            out.append(("Fee", "the fee paid" + (", charged even though it failed" if failed else ""), fee))
        if timeline:
            out.append(("What happened next", "what the sender's other transactions show; this is often what the "
                        "reader most needs to know", timeline))
        retried_ok = _ids(bundle, "timeline", where=lambda e: e.data.get("pattern") in ("retried_ok",
                                                                                       "approved_then_ok"))
        if failed and retried_ok:  # the steps of the failure's rule assume nothing happened since (D63)
            out.append(("What to do", "the same operation already succeeded later: say there is nothing to redo, "
                        "and that the reader can check that later transaction; do not repeat the conclusion's steps "
                        "about trying again or contacting support", retried_ok))
        elif failed and diagnosis:
            out.append(("What to do", "only the next steps for a non-technical reader in the conclusion; nothing "
                        "technical", diagnosis))
    else:
        out.append(("Summary", "status, block, sender, contract called", overview + _ids(bundle, "cross_check")))
        if fee:
            out.append(("Fee", "the fee paid", fee))
        call = _ids(bundle, "call")
        if call and mode == "developer":
            out.append(("Decoded call", "the function, each argument, and where the ABI came from", call))
        inner = _ids(bundle, "internal_call", "event")
        if (moves or inner) and mode == "developer":
            out.append(("Movements", "token transfers, internal calls and events, with exact values", moves + inner))
        if failed and (diagnosis or revert):
            out.append(("Cause", "each conclusion with its label; a code line only when a fact names it",
                        diagnosis + revert + _ids(bundle, "source")))
        code = _ids(bundle, "code")
        if code:
            out.append(("Code" if mode == "developer" else "Behavior and permissions",
                        "what the called function does, from the lines shown only" if mode == "developer" else
                        "what the shown lines change, and the modifiers that guard the function, by name", code))
        security = _ids(bundle, "security_note")
        if security:
            out.append(("Security notes", "each note with its line, in its own wording, as a heuristic", security))
        elif mode == "auditor" and code:
            out.append(("Security notes", "no listed pattern was found in the code shown, which does not mean it "
                        "is safe", code))
        if timeline:
            out.append(("Timeline", "the sender's other attempts", timeline))
        gas = _ids(bundle, "gas_note")
        if gas and mode == "developer":
            out.append(("Gas", "the gas notes, as heuristics", gas))
        if failed and diagnosis:
            out.append(("Next steps", "the next steps for a developer in the conclusion", diagnosis))
    if bundle.gaps:
        out.append(("What is missing", "what could not be known and what would be needed (the gaps list)", []))
    return out


def render(items: list[tuple[str, str, list[str]]]) -> str:
    lines = ["Outline for this answer: write these sections, in this order, with these headings, and nothing else. "
             "Every sentence ends with the id of the fact it states, like [E3]; the facts listed for a section are "
             "the ones it must use."]
    for i, (heading, what, ids) in enumerate(items, 1):
        lines.append(f"{i}. {heading}: {what}." + (f" Facts: {', '.join(ids)}." if ids else ""))
    return "\n".join(lines)
