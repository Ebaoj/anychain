"""Triage (PHASE4 T1, R1, the original plan's 4.5): before concluding, at most one clarifying question, asked only
when the reader's answer changes what the tool can say. The question and its options are chosen by code from the
input and the evidence (never by the model), so each case is testable; the model only writes the final answer.

The three cases of the plan:
  which_transaction  the input is not a transaction hash: an address (its latest transactions to pick from) or text
  expected_receipt   it succeeded, but the reader says they did not receive it: which payment did they expect
  intent             the cause rests on words borrowed from a known contract (an unverified contract's revert text):
                     what was the reader trying to do; "something else" means the borrowed reading does not apply

The answer becomes a `triage` fact whose source is the reader, never a fact about the chain; an answer that removes
the reason for a LIKELY reading lowers it to UNKNOWN, and nothing ever raises a label.
"""
import re
from dataclasses import asdict, dataclass, field

from anychain.models import EvidenceBundle, Evidence, Source

HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
MAX_OPTIONS = 10
# "did not receive", "never arrived", "não recebi", "não chegou", "ainda não caiu"...
NOT_RECEIVED = re.compile(
    r"\b(n[ãa]o|nunca|ainda\s+n[ãa]o|didn'?t|did\s+not|never|haven'?t|have\s+not|hasn'?t|has\s+not|not)\b"
    r"(\W+\w+){0,3}?\W+(receb\w*|cheg\w*|ca[íi]\w*|receiv\w*|arriv\w*|got)\b", re.IGNORECASE)
# Rules whose LIKELY reading rests on words borrowed from a known contract, and what that contract's check is.
BORROWED = {"slippage": "a swap's minimum-output (slippage) check",
            "deadline": "a swap's or trade's time limit (deadline) check"}
READER = Source(kind="reader", label="The reader's answer to the clarifying question")


@dataclass
class Option:
    id: str
    label: str


@dataclass
class Clarify:
    kind: str  # which_transaction | expected_receipt | intent
    question: str
    options: list[Option] = field(default_factory=list)
    free_text: str | None = None  # a placeholder when a typed answer is accepted too

    def to_dict(self) -> dict:
        return asdict(self)


def for_input(text: str, explorer) -> Clarify | None:
    """The input is not a hash: an address gets its latest transactions to pick from; anything else, the hash."""
    value = (text or "").strip()
    if HASH.match(value):
        return None
    paste = "Transaction hash (0x followed by 64 hexadecimal characters)"
    if not ADDRESS.match(value):
        return Clarify("which_transaction", "That is not a valid transaction hash. Which transaction should I explain? "
                       "Paste its hash (0x followed by 64 hexadecimal characters).", [], paste)
    try:
        rows = explorer.address_transactions(value)
    except Exception as exc:  # any failure of the explorer: say so, ask for the hash
        return Clarify("which_transaction", f"{value} is an address, not a transaction. Its list of transactions "
                       f"could not be read from the explorer ({type(exc).__name__}); paste the transaction's hash.",
                       [], paste)
    options = [Option(r.hash, f"{r.timestamp or 'time unknown'} · {r.method or 'call'} · "
                              f"{r.result or 'status unknown'} · to {r.to or '(contract creation)'}")
               for r in rows[:MAX_OPTIONS] if r.hash and HASH.match(r.hash)]
    if not options:
        return Clarify("which_transaction", f"{value} is an address, not a transaction, and the explorer lists no "
                       "transaction for it. Paste the transaction's hash.", [], paste)
    return Clarify("which_transaction", f"{value} is an address, not a transaction. Which of its latest "
                   "transactions should I explain?", options, "Or paste a transaction hash")


def for_bundle(bundle: EvidenceBundle, question: str | None, cfg) -> Clarify | None:
    """The one question the evidence calls for, or None."""
    if cfg.assistant.max_clarifying_questions < 1:
        return None
    movements = _movements(bundle)
    if bundle.status == "success" and question and NOT_RECEIVED.search(question) and movements:
        return Clarify("expected_receipt", "Which payment did you expect? Pick the one below, or type the address "
                       "that should have received it.",
                       [Option(e.id, e.text) for e, _to in movements[:MAX_OPTIONS]], "The receiving address (0x…)")
    own = _own_diagnosis(bundle)
    if bundle.status == "failed" and own is not None and own.data.get("label") == "LIKELY" \
            and own.data.get("rule") in BORROWED:
        check = BORROWED[own.data["rule"]]
        return Clarify("intent", f"What were you trying to do? The failure's words are those of {check}, but this "
                       "contract's own code decides what they mean here.",
                       [Option("swap", "Swap or trade tokens"), Option("other", "Something else")])
    return None


def apply_answer(bundle: EvidenceBundle, kind: str, answer: str) -> Evidence | None:
    """Adds the reader's answer to the evidence, as what the reader said, with what follows from the facts."""
    answer = (answer or "").strip()
    if kind == "expected_receipt":
        return _expected_receipt(bundle, answer)
    if kind == "intent":
        return _intent(bundle, answer)
    return None  # which_transaction: the answer is the hash itself


def _movements(bundle: EvidenceBundle) -> list[tuple[Evidence, str]]:
    """Every movement of value the evidence lists, with its recipient: token transfers, native movements, and the
    native value the call itself sent (Phase 4 review: native value counts as a payment too)."""
    out = [(e, e.data.get("to")) for e in bundle.items if e.kind in ("token_transfer", "native_transfer")
           and e.data.get("to")]
    overview = next((e for e in bundle.items if e.kind == "overview"), None)
    if overview is not None and overview.data.get("to") and str(overview.data.get("value")) not in ("0", "unknown", "None"):
        out.append((overview, overview.data["to"]))
    return out


def _expected_receipt(bundle: EvidenceBundle, answer: str) -> Evidence | None:
    movements = _movements(bundle)
    sources = _unique_sources([e for e, _to in movements])
    picked = next((e for e, _to in movements if e.id == answer), None)
    if picked is not None:
        to = next(to for e, to in movements if e is picked)
        return bundle.add("triage", f"The reader picked {picked.id} as the payment they expected: in this "
                          f"transaction it went to {to} ({picked.id}).", [READER] + sources,
                          {"kind": "expected_receipt", "answer": answer, "matches": [picked.id]},
                          confidence="candidate")
    if not ADDRESS.match(answer):
        return None
    to_it = [e for e, to in movements if to.lower() == answer.lower()]
    ids = ", ".join(e.id for e, _to in movements)
    if to_it:
        text = (f"The reader expected a payment to {answer}: of the movements of value the explorer lists for this "
                f"transaction ({ids}), {', '.join(e.id for e in to_it)} went to that address.")
    else:
        others = sorted({to for _e, to in movements})
        text = (f"The reader expected a payment to {answer}: none of the movements of value the explorer lists for "
                f"this transaction ({ids}) went to that address; they went to {', '.join(others)}.")
        # what the comparison cannot see, said so it is never read as "nothing went there"
        limits = ["tokens the explorer hides (for example those it marks as scam) are not listed"]
        if any(g.what == "Token transfers" for g in bundle.gaps):
            limits.insert(0, "the token-transfer list is incomplete (see its gap), so a transfer to it may be "
                             "missing")
        if any(e.kind == "internal_call" and e.data.get("moves_value") for e in bundle.items):
            limits.append("native value moved inside internal calls is not compared")
        text += " Limits of this comparison: " + "; ".join(limits) + "."
    return bundle.add("triage", text, [READER] + sources,
                      {"kind": "expected_receipt", "answer": answer, "matches": [e.id for e in to_it]},
                      confidence="candidate")


def _intent(bundle: EvidenceBundle, answer: str) -> Evidence | None:
    own = _own_diagnosis(bundle)
    if own is None or answer not in ("swap", "other") or own.data.get("rule") not in BORROWED:
        return None
    check = BORROWED[own.data["rule"]]
    if answer == "swap":
        text = (f"The reader says they were trying to swap or trade tokens. That fits the reading in {own.id} (the "
                f"words of {check}); it stays LIKELY because this contract's own code decides what they mean.")
    else:
        text = (f"The reader says they were not swapping or trading tokens. That does not fit the reading in "
                f"{own.id}: its words are those of {check}, and here their meaning is in this contract's own code, "
                "which the evidence does not show.")
    fact = bundle.add("triage", text, [READER], {"kind": "intent", "answer": answer, "diagnosis": own.id},
                      confidence="candidate")
    if answer == "other" and own.data.get("label") == "LIKELY":  # the borrowed reading no longer holds: lowered
        own.data["label"] = "UNKNOWN"
        own.text = re.sub(r"^LIKELY:", f"UNKNOWN (after the reader's answer, {fact.id}):", own.text)
    return fact


def _own_diagnosis(bundle: EvidenceBundle) -> Evidence | None:
    return next((e for e in bundle.items if e.kind == "diagnosis" and not e.data.get("from_replay")), None)


def _unique_sources(facts: list[Evidence]) -> list[Source]:
    out: list[Source] = []
    for e in facts:
        for s in e.sources:
            if s not in out:
                out.append(s)
    return out
