"""LLM writer: turns an evidence bundle into a readable explanation that cites [E#] ids."""
import json
import os
from pathlib import Path

from anychain.config import AppConfig
from anychain.models import EvidenceBundle

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class WriterError(Exception):
    """The LLM could not be used. The caller falls back to the deterministic output."""


def load_prompt(mode: str, language: str) -> str:
    base = (PROMPTS_DIR / "base.md").read_text()
    mode_text = (PROMPTS_DIR / f"{mode}.md").read_text()
    return f"{base}\n\n{mode_text}\n\nWrite the answer in this language: {language}."


def evidence_payload(bundle: EvidenceBundle) -> str:
    """The only information the model is allowed to use."""
    return json.dumps(
        {
            "network": bundle.network,
            "tx_hash": bundle.tx_hash,
            "status": bundle.status,
            "evidence": [{"id": e.id, "kind": e.kind, "fact": e.text} for e in bundle.items],
            "gaps": [g.model_dump() for g in bundle.gaps],
        },
        ensure_ascii=False,
        indent=1,
    )


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
            messages=[{"role": "user", "content": evidence_payload(bundle)}],
        )
    except Exception as exc:
        raise WriterError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
