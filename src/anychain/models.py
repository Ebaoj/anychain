"""Data shapes shared across the pipeline: evidence facts and the bundle."""
from typing import Literal

from pydantic import BaseModel, Field

# Why a gap exists. The event log counts these per network; only some of them are problems:
#   source_unavailable  a source did not answer (timeout, 5xx, rate limit) -> alert when the rate rises
#   source_error        a source refused (4xx, node error) or sent something unreadable -> alert
#   source_behind       the explorer has not indexed it yet, or disagrees with the node -> alert if persistent
#   processing_error    our code failed on this payload, or our arithmetic contradicts a source -> always alert
#   config_error        the network config does not fit what the sources report (wrong chain id or type) -> alert
#   pending             the transaction is not mined yet -> expected
#   not_interpretable   no ABI, list truncated, unknown field, needs a source we do not have -> expected limit
# Note: "not found" (a hash neither source knows) is not_interpretable: usually a typo or the wrong network.
GapCause = Literal["source_unavailable", "source_error", "source_behind", "processing_error", "config_error",
                   "pending", "not_interpretable"]


# How sure a fact is (PHASE2 R6, D29):
#   confirmed      every source is the network's own node (the source of record for chain state), or the
#                  fact is a check of the explorer against the node (set on purpose, citing both)
#   single_source  from the explorer only (an indexer of the node; not cross-checked fact by fact)
#   candidate      inferred, e.g. a selector matched in a public signature database; set only on purpose
Confidence = Literal["confirmed", "single_source", "candidate"]


class Source(BaseModel):
    """Where a fact came from, so a reader can check it."""

    kind: str  # explorer_ui | explorer_api | rpc | repo
    label: str
    url: str | None = None  # clickable link when one exists
    detail: str | None = None  # e.g. JSON-RPC method + params


class Evidence(BaseModel):
    """One numbered fact. The LLM may only cite these ids."""

    id: str
    kind: str
    text: str
    data: dict = Field(default_factory=dict)
    sources: list[Source] = []
    confidence: Confidence = "single_source"


class Gap(BaseModel):
    """Something we could not get, and what would fix it."""

    what: str
    why: str
    needed: str
    retryable: bool = False  # True: network trouble, trying again later may fill it
    cause: GapCause = "not_interpretable"


class EvidenceBundle(BaseModel):
    network: str
    tx_hash: str
    status: str  # success | failed | pending | dropped | unknown
    items: list[Evidence] = []
    gaps: list[Gap] = []
    abi_sources: dict[str, str] = {}  # address -> where its ABI came from

    def add(self, kind: str, text: str, sources: list[Source], data: dict | None = None,
            confidence: Confidence | None = None) -> Evidence:
        """`confidence` follows the sources unless set: a fact whose every source is the node is
        confirmed; one that also rests on the explorer (e.g. its ABI) is single_source. "candidate" is
        only ever set on purpose, and nothing upgrades it."""
        if confidence is None:  # confirmed only when every source is the node
            confidence = "confirmed" if sources and all(s.kind == "rpc" for s in sources) else "single_source"
        ev = Evidence(id=f"E{len(self.items) + 1}", kind=kind, text=text, data=data or {}, sources=sources,
                      confidence=confidence)
        self.items.append(ev)
        return ev

    def add_gap(self, what: str, why: str, needed: str, retryable: bool,
                cause: GapCause = "not_interpretable") -> None:
        self.gaps.append(Gap(what=what, why=why, needed=needed, retryable=retryable, cause=cause))

    def ids(self) -> set[str]:
        return {e.id for e in self.items}
