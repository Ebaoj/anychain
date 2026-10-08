"""The structured answer of the original plan (section 4.4), printed by `explain --json` (PHASE2_5 T6, R4, D43).

Built from the evidence bundle and the written text only: every field is a view of numbered facts, and
each list item keeps the id of the fact it comes from, so a reader (or the API of Phase 3) can check it.
"""
from anychain.diagnosis import NO_REASON_STEPS, SUPPORT_STEPS
from anychain.models import EvidenceBundle

SCHEMA = "anychain.answer/1"
CALLS = ("call", "internal_call")
TRANSFERS = ("token_transfer", "native_transfer")


def structured_answer(bundle: EvidenceBundle, summary: str | None, summary_status: str, mode: str | None) -> dict:
    """`summary`: the checked written answer, or None (withheld, unavailable, skipped: `summary_status` says which)."""
    items = bundle.items
    diagnosis = [_diagnosis(e, bundle) for e in items if e.kind == "diagnosis"]
    own = next((d for d in diagnosis if not d["from_replay"]), diagnosis[0] if diagnosis else None)
    if own:
        by_reader, label = own["next_steps"], own["label"]
    elif bundle.status == "failed":  # a failure with no conclusion at all (the event log says UNKNOWN too)
        by_reader, label = {"support": SUPPORT_STEPS["no_reason"], "developer": NO_REASON_STEPS}, "UNKNOWN"
    else:
        by_reader, label = {"support": [], "developer": []}, None
    return {
        "schema": SCHEMA,
        "network": bundle.network,
        "tx_hash": bundle.tx_hash,
        "status": bundle.status,
        "mode": mode,
        "summary": summary,
        "summary_status": summary_status,
        "calls": [{"id": e.id, "top_level": e.kind == "call", "function": e.data.get("function"),
                   "args": e.data.get("args"), "text": e.text, "confidence": e.confidence}
                  for e in items if e.kind in CALLS],
        "transfers": _transfers(bundle),
        "events": [{"id": e.id, "event": e.data.get("event"), "args": e.data.get("args"), "text": e.text,
                    "confidence": e.confidence} for e in items if e.kind == "event"],
        "diagnosis": [{k: v for k, v in d.items() if k != "next_steps"} for d in diagnosis],
        "next_steps": by_reader["support"] if mode == "support" else by_reader["developer"],
        "next_steps_by_reader": by_reader,
        "security_notes": [{"id": e.id, "code": e.data.get("code"), "notes": e.data.get("notes"), "text": e.text}
                           for e in items if e.kind == "security_note"],
        "confidence": {**_status_confidence(bundle), "diagnosis": label},
        "sources": _sources(bundle),
        "gaps": [g.model_dump() for g in bundle.gaps],
        "evidence": [e.model_dump() for e in items],
    }


def _diagnosis(e, bundle: EvidenceBundle) -> dict:
    """A conclusion with the ids of the facts it rests on: itself, its reads, its replay, and, for a conclusion
    drawn from the decoded reason, the reason; for a deadline comparison, the call holding the parameter."""
    steps = e.data.get("next_steps") or {}
    facts = [e.id] + list(e.data.get("reads") or []) + ([e.data["replay"]] if e.data.get("replay") else [])
    if not e.data.get("from_replay"):
        facts += [r.id for r in bundle.items if r.kind == "revert"]
    if (e.data.get("computed") or {}).get("deadline_check"):
        facts += [c.id for c in bundle.items if c.kind == "call"]
    return {"id": e.id, "label": e.data.get("label"), "rule": e.data.get("rule"),
            "from_replay": bool(e.data.get("from_replay")), "text": e.text, "confidence": e.confidence,
            "facts": list(dict.fromkeys(facts)),
            "next_steps": {"support": list(steps.get("support") or []), "developer": list(steps.get("developer") or [])}}


def _transfers(bundle: EvidenceBundle) -> list[dict]:
    """Value that moved: token transfers, native transfers, and for a transaction that succeeded, the native value
    it carried (the overview's) and native value moved by its internal calls (not the ones said to be the same
    movement as another fact). A reverted transaction moved no value: its overview says so."""
    out = [{"id": e.id, "kind": e.kind, "text": e.text, "confidence": e.confidence, "data": e.data}
           for e in bundle.items if e.kind in TRANSFERS]
    if bundle.status != "success":
        return out
    for e in bundle.items:
        if e.kind == "overview" and str(e.data.get("value") or "0") not in ("0", "0.0"):
            out.append({"id": e.id, "kind": "transaction_value", "text": e.text, "confidence": e.confidence,
                        "data": {k: e.data.get(k) for k in ("from", "to", "value")}})
        elif e.kind == "internal_call" and e.data.get("moves_value") and not e.data.get("same_as"):
            out.append({"id": e.id, "kind": "internal_value", "text": e.text, "confidence": e.confidence,
                        "data": e.data})
    return out


def _status_confidence(bundle: EvidenceBundle) -> dict:
    """The status's confidence, and what it rests on: the node's receipt compared with the explorer's (confirmed or
    disputed), or one source only (the overview's own confidence)."""
    check = next((e for e in bundle.items if e.kind == "cross_check" and "agrees" in e.data), None)
    if check is not None:
        agrees = check.data["agrees"]
        return {"status": "confirmed" if agrees else "disputed",
                "status_basis": "node_and_explorer_agree" if agrees else "node_and_explorer_disagree"}
    overview = next((e for e in bundle.items if e.kind == "overview"), None)
    if overview is None:
        return {"status": None, "status_basis": None}
    node_only = all(s.kind == "rpc" for s in overview.sources)
    return {"status": overview.confidence, "status_basis": "node_only" if node_only else "explorer_only"}


def _sources(bundle: EvidenceBundle) -> list[dict]:
    """Every distinct linked source, with the ids of the facts that cite it."""
    seen: dict[tuple, dict] = {}
    for e in bundle.items:
        for s in e.sources:
            if not s.url:
                continue
            entry = seen.setdefault((s.kind, s.url), {"kind": s.kind, "label": s.label, "url": s.url, "facts": []})
            if e.id not in entry["facts"]:
                entry["facts"].append(e.id)
    return list(seen.values())
