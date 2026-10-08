"""Deterministic markdown output of an evidence bundle (works with no LLM)."""
from anychain.models import EvidenceBundle, Source

MAX_DETAIL = 160


def _source_text(source: Source) -> str:
    """A clickable link when there is a URL, plus the exact call made (e.g. RPC method and params)."""
    text = f"[{source.label}]({source.url})" if source.url else source.label
    if source.detail:
        detail = source.detail if len(source.detail) <= MAX_DETAIL else source.detail[:MAX_DETAIL] + "..."
        text += f" `{detail}`"
    return text


CONFIDENCE_TEXT = {"confirmed": "confirmed by the node", "single_source": "explorer only",
                   "candidate": "candidate: inferred, not confirmed"}


def render_markdown(bundle: EvidenceBundle) -> str:
    lines = [
        f"# Transaction {bundle.tx_hash}",
        f"Network: **{bundle.network}** | Status: **{bundle.status}**",
        "",
        "## Evidence",
    ]
    for ev in bundle.items:
        links = " ".join(_source_text(s) for s in ev.sources)
        lines.append(f"- **[{ev.id}]** {ev.text} _({CONFIDENCE_TEXT[ev.confidence]})_  \n  _Sources:_ {links}")
    if bundle.abi_sources:
        lines += ["", "## ABI sources"] + [f"- `{a}`: {s}" for a, s in bundle.abi_sources.items()]
    if bundle.gaps:
        lines += ["", "## Missing data"]
        for g in bundle.gaps:
            retry = " _(temporary: trying again later may fill this)_" if g.retryable else ""
            lines.append(f"- **{g.what}**: {g.why}. _Needed:_ {g.needed}{retry}")
    return "\n".join(lines) + "\n"
