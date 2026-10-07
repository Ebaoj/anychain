"""LLM writer: turns an evidence bundle into a readable explanation that cites [E#] ids."""
import json
import os
import re

import httpx
from pathlib import Path

from anychain.config import AppConfig
from anychain.models import EvidenceBundle, Source

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class WriterError(Exception):
    """The LLM could not be used. The caller falls back to the deterministic output."""


def load_prompt(mode: str, language: str) -> str:
    base = (PROMPTS_DIR / "base.md").read_text()
    mode_text = (PROMPTS_DIR / f"{mode}.md").read_text()
    return f"{base}\n\n{mode_text}\n\nWrite the answer in this language: {language}."


def evidence_payload(bundle: EvidenceBundle, cfg: AppConfig | None = None) -> str:
    """The only information the model is allowed to use. The RPC node's address never goes in."""
    hidden = (httpx.URL(cfg.rpc.url).host,) if cfg else ()
    return json.dumps(
        {
            "network": bundle.network,
            "tx_hash": bundle.tx_hash,
            "status": bundle.status,
            "evidence": [
                {"id": e.id, "kind": e.kind, "fact": e.text, "sources": _sources_for_llm(e.sources)}
                for e in bundle.items
            ],
            "gaps": [{k: _without_urls(v, hidden) if isinstance(v, str) else v for k, v in g.model_dump().items()}
                     for g in bundle.gaps],
        },
        ensure_ascii=False,
        indent=1,
    )


URL_RE = re.compile(r"https?://\S+")


def _without_urls(text: str, hidden_hosts: tuple[str, ...] = ()) -> str:
    """Gap messages can quote the endpoint that failed (possibly internal); the model gets none of them."""
    text = URL_RE.sub("[endpoint]", text)
    for host in hidden_hosts:
        text = text.replace(host, "[endpoint]")
    return text


def _sources_for_llm(sources: list[Source]) -> list[dict]:
    """What the model may cite. Only explorer pages keep their URL: API and RPC URLs
    can be internal (e.g. a private network's node) and are no use to a reader."""
    out = []
    for s in sources:
        item = {"label": s.label}
        if s.kind == "explorer_ui" and s.url:
            item["url"] = s.url
        if s.detail:
            item["detail"] = s.detail
        if item not in out:
            out.append(item)
    return out


def write_explanation(bundle: EvidenceBundle, cfg: AppConfig, mode: str, client=None) -> str:
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise WriterError("ANTHROPIC_API_KEY is not set")
        import anthropic

        client = anthropic.Anthropic()
    try:
        msg = client.messages.create(
            model=cfg.llm.model,
            max_tokens=cfg.llm.max_tokens,
            temperature=cfg.llm.temperature,
            system=load_prompt(mode, cfg.assistant.language),
            messages=[{"role": "user", "content": evidence_payload(bundle, cfg)}],
        )
    except Exception as exc:
        raise WriterError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
