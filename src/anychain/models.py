"""Data shapes shared across the pipeline: evidence facts and the bundle."""
from pydantic import BaseModel, Field


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


class Gap(BaseModel):
    """Something we could not get, and what would fix it."""

    what: str
    why: str
    needed: str


class EvidenceBundle(BaseModel):
    network: str
    tx_hash: str
    status: str  # success | failed | pending | unknown
    items: list[Evidence] = []
    gaps: list[Gap] = []
    abi_sources: dict[str, str] = {}  # address -> where its ABI came from

    def add(self, kind: str, text: str, sources: list[Source], data: dict | None = None) -> Evidence:
        ev = Evidence(id=f"E{len(self.items) + 1}", kind=kind, text=text, data=data or {}, sources=sources)
        self.items.append(ev)
        return ev

    def ids(self) -> set[str]:
        return {e.id for e in self.items}
