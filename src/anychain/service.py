"""One answer to "explain this transaction", shared by the command line and the API (PHASE3 T2, D46).

The steps, in order: the evidence (from the cache when it is kept, D44), then, when asked, the written
answer (from the cache, or written and checked, D28), then one row in the event log. Everything a caller
prints or returns comes from `Answered`; nothing here prints.
"""
import time
from dataclasses import dataclass, field

from anychain.answer import structured_answer
from anychain.cache import answer_key, cached_bundle
from anychain.events import CheckEvent, RunEvent
from anychain.models import EvidenceBundle
from anychain.writer import CheckedAnswer, WriterError, clean_question

WRITE, SKIP, NONE = "write", "skip", "none"  # write the answer; skip it (asked not to); not asked (evidence only)


class Crash(Exception):
    """Collecting the evidence failed in a way the tool does not handle (logged as a crash)."""


@dataclass
class Answered:
    bundle: EvidenceBundle
    mode: str
    cache: str  # the evidence's cache state (hit, stored, off, not kept: …)
    text: str | None = None  # the checked written answer, or None
    summary_status: str = "skipped"  # ok, retried, withheld, unavailable, cached, skipped, no_evidence
    problems: list[str] = field(default_factory=list)  # what the answer check found
    writer_error: str | None = None  # why no model could write it
    run_id: int | None = None  # the event log row, for feedback
    notes: list[str] = field(default_factory=list)  # side problems to tell the user (the answer is still given)
    question: str | None = None  # the reader's question given with the hash (R12)

    def structured(self) -> dict:
        return {**structured_answer(self.bundle, self.text, self.summary_status, self.mode),
                "question": self.question, "run_id": self.run_id}


def answer_transaction(tx_hash: str, cfg, mode: str, *, write: str, fresh: bool, source: str, log, store,
                       build, write_fn, finality, record, question: str | None = None) -> Answered:
    """Raises InvalidHashError for a malformed hash, Crash for anything unexpected (already logged)."""
    from anychain.bundle import InvalidHashError
    started = time.monotonic()
    tx_hash = tx_hash.strip()
    try:
        bundle, cache_state = cached_bundle(tx_hash, cfg, store, build, finality, fresh)
    except InvalidHashError:
        raise
    except Exception as exc:  # last line of defence: never a stack trace for the user
        record(log, RunEvent.crash(cfg.network.name, tx_hash, source, _ms(started), f"{type(exc).__name__}: {exc}"))
        raise Crash(f"{type(exc).__name__}: {exc}") from exc
    question = clean_question(question)  # the same text is keyed, echoed and sent to the model
    result = Answered(bundle, mode, cache_state, question=question)

    def event(**kw) -> RunEvent:
        return RunEvent.from_bundle(bundle, source, _ms(started), cache=cache_state, mode=mode, **kw)
    if write == NONE:
        result.run_id = record(log, event())
        return result
    if write == SKIP or not bundle.items:
        result.summary_status = "skipped" if write == SKIP else "no_evidence"
        result.run_id = record(log, event(writer="skipped"))
        return result
    final = cache_state in ("hit", "stored")  # a written answer is kept only for evidence that cannot change
    key = answer_key(bundle, cfg, mode, question) if final else None
    kept = None
    if key and not fresh:
        try:
            kept = store.get_answer(key)
        except Exception:  # a cache that cannot be read is skipped
            kept = None
    try:
        checked = CheckedAnswer(kept[0], "cached", []) if kept else (write_fn(bundle, cfg, mode, question=question) if question
                                                                     else write_fn(bundle, cfg, mode))
    except WriterError as exc:
        result.summary_status, result.writer_error = "unavailable", str(exc)
        result.run_id = record(log, event(writer="unavailable", usage=exc.usage))
        return result
    # What the model stated outside the evidence, kept for review: "retried" (fixed) or "fail" (withheld).
    check = (CheckEvent("answer_check", "fail" if checked.text is None else "retried", "; ".join(checked.problems)),
             ) if checked.problems else ()
    result.run_id = record(log, event(checks=check, writer=checked.outcome, usage=checked.usage))
    if key and checked.text is not None and not kept:
        try:
            store.put_answer(key, checked.text, checked.outcome)
        except Exception as exc:  # the answer was given; only its reuse is lost
            result.notes.append(f"(cache unavailable: {type(exc).__name__}: {exc})")
    result.text, result.problems = checked.text, list(checked.problems)
    result.summary_status = {"fail": "withheld"}.get(checked.outcome, checked.outcome)
    return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
