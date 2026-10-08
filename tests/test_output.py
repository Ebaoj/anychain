import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from anychain.render import render_markdown
from anychain.writer import (AnthropicBackend, ClaudeCodeBackend, OpenAIBackend, WriterError, backend_for,
                             evidence_payload, write_explanation)
from tests.conftest import USDC_TX, replay_bundle


class FakeClient:
    """Stands in for the Anthropic SDK; records what it was sent."""

    def __init__(self, reply="ok [E1]", fail=False):
        self.reply, self.fail, self.sent = reply, fail, None
        self.messages = self

    def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.sent = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


def test_markdown_lists_ids_and_links(eth_cfg):
    md = render_markdown(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer"))
    assert "**[E1]**" in md and "https://eth.blockscout.com/tx/" in md
    assert f"`eth_getTransactionReceipt [{USDC_TX}]`" in md  # the exact RPC call, not just the node URL


def test_writer_sends_only_evidence_and_uses_config_model(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    client = FakeClient()
    assert write_explanation(bundle, eth_cfg, "support", AnthropicBackend(eth_cfg.llm, client)) == "ok [E1]"
    assert client.sent["model"] == eth_cfg.llm.model
    assert client.sent["messages"][0]["content"] == evidence_payload(bundle, eth_cfg)
    payload = json.loads(evidence_payload(bundle))
    assert payload["evidence"][0]["sources"][0]["url"].startswith("https://eth.blockscout.com/tx/")
    every_url = [src.get("url") for ev in payload["evidence"] for src in ev["sources"] if src.get("url")]
    assert all(u.startswith("https://eth.blockscout.com/tx/") for u in every_url)  # no API or RPC URLs
    assert any("eth_getTransactionReceipt" in src.get("detail", "") for ev in payload["evidence"] for src in ev["sources"])
    assert "pt-BR" in client.sent["system"] and "Mode: support" in client.sent["system"]


def test_writer_failure_is_a_writer_error(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    with pytest.raises(WriterError):
        write_explanation(bundle, eth_cfg, "developer", AnthropicBackend(eth_cfg.llm, FakeClient(fail=True)))


def test_gap_texts_reach_the_llm_without_endpoint_urls(eth_cfg):
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer", offline_hosts={"ethereum-rpc.publicnode.com"})
    assert any("publicnode" in g.why for g in bundle.gaps)  # the user-facing gap names the endpoint...
    payload = evidence_payload(bundle, eth_cfg)
    assert "publicnode" not in payload and "[endpoint]" in payload  # ...the model does not see it



# ---- the three backends (D27) ------------------------------------------------------------

def test_default_backend_is_claude_code(eth_cfg):
    assert eth_cfg.llm.provider == "claude_code" and isinstance(backend_for(eth_cfg.llm), ClaudeCodeBackend)


def _cli_answer(stdout, returncode=0, stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_claude_code_is_isolated_and_gets_the_evidence_on_stdin(eth_cfg, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw, cwd_files=os.listdir(kw["cwd"]))
        return _cli_answer(json.dumps({"type": "result", "is_error": False, "result": "ok [E1]"}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stray-key-not-real")  # e.g. loaded from an old .env
    monkeypatch.setattr("anychain.writer.shutil.which", lambda _c: "/usr/local/bin/claude")
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    out = write_explanation(bundle, eth_cfg, "support", ClaudeCodeBackend(eth_cfg.llm, run=run))
    assert out == "ok [E1]"
    argv = seen["argv"]
    for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--model") + 1] == eth_cfg.llm.model
    assert "Mode: support" in argv[argv.index("--system-prompt") + 1]
    assert seen["input"] == evidence_payload(bundle, eth_cfg)  # the facts, and nothing else
    assert seen["cwd_files"] == [] and seen["cwd"] != str(Path.cwd())  # an empty directory, not the project
    assert "ANTHROPIC_API_KEY" not in seen["env"] and "PATH" in seen["env"]  # the login is used, not a stray key


def test_claude_code_errors_become_writer_errors(eth_cfg, monkeypatch):
    import subprocess
    monkeypatch.setattr("anychain.writer.shutil.which", lambda _c: "/usr/local/bin/claude")
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    cases = [
        lambda *a, **k: _cli_answer(json.dumps({"is_error": True, "result": "Not logged in"})),
        lambda *a, **k: _cli_answer("", 1, "command failed"),
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("claude", 1)),
        lambda *a, **k: _cli_answer(json.dumps(["not", "a", "dict"])),
        lambda *a, **k: _cli_answer(json.dumps({"is_error": False, "result": None})),
    ]
    for run in cases:
        with pytest.raises(WriterError):
            write_explanation(bundle, eth_cfg, "support", ClaudeCodeBackend(eth_cfg.llm, run=run))
    monkeypatch.setattr("anychain.writer.shutil.which", lambda _c: None)
    with pytest.raises(WriterError, match="not found"):
        write_explanation(bundle, eth_cfg, "support", ClaudeCodeBackend(eth_cfg.llm))


def test_openai_backend_request_and_reply(eth_cfg, monkeypatch):
    import httpx
    seen = {}

    def handler(request):
        seen["auth"], seen["body"] = request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok [E2]"}}]})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    llm = eth_cfg.llm.model_copy(update={"provider": "openai", "model": "some-openai-model"})
    backend = OpenAIBackend(llm, httpx.Client(transport=httpx.MockTransport(handler)))
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    assert write_explanation(bundle, eth_cfg, "support", backend) == "ok [E2]"
    body = seen["body"]
    assert seen["auth"] == "Bearer test-key-not-real" and body["model"] == "some-openai-model"
    assert body["max_completion_tokens"] == llm.max_tokens and "max_tokens" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"] == evidence_payload(bundle, eth_cfg)


def test_api_backends_need_their_key(eth_cfg, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    for backend in (OpenAIBackend(eth_cfg.llm), AnthropicBackend(eth_cfg.llm)):
        with pytest.raises(WriterError, match="API_KEY is not set"):
            write_explanation(bundle, eth_cfg, "support", backend)


def test_openai_failures_are_writer_errors_without_the_key(eth_cfg, monkeypatch):
    import httpx
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-not-real-abcd")
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    replies = [
        httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-proj-****abcd",
                                            "type": "invalid_request_error"}}),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None}}]}),
    ]
    for reply in replies:
        backend = OpenAIBackend(eth_cfg.llm, httpx.Client(transport=httpx.MockTransport(lambda _r, r=reply: r)))
        with pytest.raises(WriterError) as err:
            write_explanation(bundle, eth_cfg, "support", backend)
        assert "abcd" not in str(err.value)


def test_openai_temperature_can_be_left_to_the_model(eth_cfg, monkeypatch):
    import httpx
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    llm = eth_cfg.llm.model_copy(update={"provider": "openai", "temperature": None})
    write_explanation(replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer"), eth_cfg, "support",
                      OpenAIBackend(llm, httpx.Client(transport=httpx.MockTransport(handler))))
    assert "temperature" not in seen
