"""LLM writer: turns an evidence bundle into a readable explanation that cites [E#] ids.

The model is reached through one of three backends, chosen by `llm.provider` (D27):
  - claude_code: the local Claude Code CLI in print mode, logged in with the user's account. No API
    key. Isolated: no tools (the model can only write from the facts it is given), no user settings,
    no MCP servers, no saved session, run from an empty directory.
  - anthropic / openai: the providers' HTTP APIs, for a deployed service. Keys come from the
    environment (ANTHROPIC_API_KEY / OPENAI_API_KEY), set by the vault or the host's secret store.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from anychain.config import AppConfig, LlmConfig
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


class LlmBackend(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class ClaudeCodeBackend:
    """`claude -p` with JSON output. temperature and max_tokens do not apply (the CLI has no such options)."""

    # Each flag checked against `claude --help` (2.1.293); measured on 2026-10-08: a call with these
    # sends about 600 tokens of overhead, without them the user's whole environment (about 200x more).
    ISOLATION = ("--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence")
    # An API key in the environment takes precedence over the claude.ai login (CLI stderr, checked
    # 2026-10-08), so a stray key would be billed, or fail if dead. The child never sees these.
    STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                    "OPENAI_API_KEY")

    def __init__(self, llm: LlmConfig, run=subprocess.run):
        self.llm, self.run = llm, run

    def complete(self, system: str, user: str) -> str:
        binary = shutil.which(self.llm.claude_code_command)
        if binary is None:
            raise WriterError(f"{self.llm.claude_code_command!r} not found: install Claude Code and log in, "
                              "or set llm.provider to anthropic or openai")
        argv = [binary, "-p", "--output-format", "json", *self.ISOLATION, "--model", self.llm.model,
                "--system-prompt", system]
        with tempfile.TemporaryDirectory() as empty:  # no project files or CLAUDE.md around it
            try:
                env = {k: v for k, v in os.environ.items() if k not in self.STRIPPED_ENV}
                done = self.run(argv, input=user, capture_output=True, text=True, cwd=empty, env=env,
                                timeout=self.llm.timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise WriterError(f"Claude Code did not answer within {self.llm.timeout_s} s") from exc
        try:
            out = json.loads(done.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            detail = (done.stderr or done.stdout or "").strip()[:300]
            raise WriterError(f"Claude Code returned no JSON (exit {done.returncode}): {detail}") from exc
        if not isinstance(out, dict) or out.get("is_error") or not isinstance(out.get("result"), str):
            reason = out.get("result") if isinstance(out, dict) else out
            raise WriterError(f"Claude Code reported an error: {str(reason)[:300]}")
        return out["result"]


class AnthropicBackend:
    def __init__(self, llm: LlmConfig, client=None):
        self.llm, self.client = llm, client

    def complete(self, system: str, user: str) -> str:
        client = self.client
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise WriterError("ANTHROPIC_API_KEY is not set")
            import anthropic
            client = anthropic.Anthropic(timeout=self.llm.timeout_s)
        msg = client.messages.create(model=self.llm.model, max_tokens=self.llm.max_tokens,
                                     temperature=self.llm.temperature, system=system,
                                     messages=[{"role": "user", "content": user}])
        return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")


class OpenAIBackend:
    """Chat Completions over HTTP (field names from the official openai-python SDK:
    `max_completion_tokens`, since `max_tokens` is deprecated)."""

    URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, llm: LlmConfig, client: httpx.Client | None = None):
        self.llm, self.client = llm, client

    def complete(self, system: str, user: str) -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise WriterError("OPENAI_API_KEY is not set")
        body = {"model": self.llm.model, "max_completion_tokens": self.llm.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.llm.temperature is not None:  # reasoning models reject any value but their default
            body["temperature"] = self.llm.temperature
        if self.client is not None:
            response = self.client.post(self.URL, headers={"Authorization": f"Bearer {key}"}, json=body)
        else:
            with httpx.Client(timeout=self.llm.timeout_s) as client:
                response = client.post(self.URL, headers={"Authorization": f"Bearer {key}"}, json=body)
        if response.status_code != 200:
            # Only the error type: OpenAI's error text can echo part of the key.
            kind = _json_get(response, "error", "type") or _json_get(response, "error", "code") or "unknown"
            raise WriterError(f"OpenAI answered HTTP {response.status_code} ({kind})")
        content = _json_get(response, "choices", 0, "message", "content")
        if not isinstance(content, str):  # null on a refusal or a tool call
            raise WriterError("OpenAI returned no text")
        return content


def _json_get(response: httpx.Response, *path):
    try:
        value = response.json()
        for step in path:
            value = value[step]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


BACKENDS = {"claude_code": ClaudeCodeBackend, "anthropic": AnthropicBackend, "openai": OpenAIBackend}


def backend_for(llm: LlmConfig) -> LlmBackend:
    return BACKENDS[llm.provider](llm)


def write_explanation(bundle: EvidenceBundle, cfg: AppConfig, mode: str, backend: LlmBackend | None = None) -> str:
    backend = backend or backend_for(cfg.llm)
    try:
        return backend.complete(load_prompt(mode, cfg.assistant.language), evidence_payload(bundle, cfg))
    except WriterError:
        raise
    except Exception as exc:
        raise WriterError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc
