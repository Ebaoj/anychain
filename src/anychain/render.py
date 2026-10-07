"""Deterministic markdown output of an evidence bundle (works with no LLM)."""
from anychain.models import EvidenceBundle


def render_markdown(bundle: EvidenceBundle) -> str:
    lines = [f"# Transaction {bundle.tx_hash}", f"Network: **{bundle.network}** | Status: **{bundle.status}**", "", "## Evidence"]
    for ev in bundle.items:
        links = " ".join(f"[{s.label}]({s.url})" if s.url else f"{s.label} ({s.detail})" for s in ev.sources)
        lines.append(f"- **[{ev.id}]** {ev.text}  \n  _Sources:_ {links}")
    if bundle.abi_sources:
        lines += ["", "## ABI sources"] + [f"- `{a}`: {s}" for a, s in bundle.abi_sources.items()]
    if bundle.gaps:
        lines += ["", "## Missing data"]
        lines += [f"- **{g.what}**: {g.why}. _Needed:_ {g.needed}" for g in bundle.gaps]
    return "\n".join(lines) + "\n"
