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
    assert client.sent["messages"][0]["content"].startswith(evidence_payload(bundle, eth_cfg, "support"))
    assert "Outline for this answer" in client.sent["messages"][0]["content"]  # the facts, then their outline (D62)
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
    assert seen["input"].startswith(evidence_payload(bundle, eth_cfg, "support"))  # the facts, then the outline
    assert "Outline for this answer" in seen["input"]
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
    assert body["messages"][1]["content"].startswith(evidence_payload(bundle, eth_cfg, "support"))


def test_api_backends_need_their_key(eth_cfg, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    bundle = replay_bundle(eth_cfg, USDC_TX, "eth_usdc_transfer")
    for backend in (OpenAIBackend(eth_cfg.llm), AnthropicBackend(eth_cfg.llm)):
        with pytest.raises(WriterError, match="API key: save one with `anychain llm set`"):
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


def test_a_support_answer_gets_only_the_readers_steps_and_an_outline_of_its_facts():
    # D62: a small model given both lists of steps mixed them (gpt-4.1-nano, support mode, 2026-10-09)
    from anychain import outline
    from anychain.config import load_config
    from tests.conftest import ROOT
    from tests.test_golden import _case
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    _c, tx = _case("eth_fail_expired_v2")
    b = replay_bundle(cfg, tx, "eth_fail_expired_v2")
    support = evidence_payload(b, cfg, "support")
    assert "Next step for a non-technical reader:" in support and "Next step for a developer:" not in support
    developer = evidence_payload(b, cfg, "developer")
    assert "Next step for a developer:" in developer and "Next step for a non-technical reader:" not in developer
    items = outline.build(b, "support")
    headings = [h for h, _w, _ids in items]
    timeline = [e.id for e in b.items if e.kind == "timeline" and e.data.get("pattern")]
    assert "What happened next" in headings and timeline[0] in dict((h, ids) for h, _w, ids in items)["What happened next"]
    _c, tx = _case("eth_usdc_transfer")
    ok = replay_bundle(cfg, tx, "eth_usdc_transfer")
    assert "What happened next" not in [h for h, _w, _ids in outline.build(ok, "support")]  # no fact, no section
    assert "What to do" not in [h for h, _w, _ids in outline.build(ok, "support")]


def test_citations_written_in_prose_are_normalized():
    from anychain.writer import normalize_citations
    # real shapes from gpt-4.1-nano answers (eval, 2026-10-09)
    assert normalize_citations("conforme E19.") == "conforme [E19]."
    assert normalize_citations("Os fatos E5 a E7 indicam") == "Os fatos [E5, E6, E7] indicam"
    assert normalize_citations("já citado [E1, E7] e (E4)") == "já citado [E1, E7] e [E4]"
    assert normalize_citations("ERC20 e E2E") == "ERC20 e E2E"  # not ids


def test_an_answer_missing_a_key_fact_is_rewritten_once_and_then_given(eth_cfg):
    from anychain.writer import write_checked
    from tests.test_golden import _case
    _c, tx = _case("eth_fail_expired_v2")
    b = replay_bundle(eth_cfg, tx, "eth_fail_expired_v2")
    pattern = next(e.id for e in b.items if e.kind == "timeline" and e.data.get("pattern"))
    diagnosis = next(e.id for e in b.items if e.kind == "diagnosis")

    class Backend:
        last_usage = None
        users = []
        answers = iter([f"It failed [E1] [{diagnosis}].", f"It failed [E1] [{diagnosis}]. It worked later [{pattern}]."])

        def complete(self, system, user):
            self.users.append(user)
            return next(self.answers)
    backend = Backend()
    checked = write_checked(b, eth_cfg, "support", backend=backend)
    assert checked.outcome == "retried" and pattern in checked.text
    assert f"must say what {pattern} says" in backend.users[1]


def test_when_the_same_operation_succeeded_later_the_support_steps_say_so():
    # real: gpt-4.1-nano said the swap succeeded 48 s later [E10], then told the merchant to contact support before
    # trying again (the deadline rule's own steps); the outline now gives one instruction, from the timeline (D63)
    from anychain import outline
    from anychain.config import load_config
    from tests.conftest import ROOT
    from tests.test_golden import _case
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    _c, tx = _case("eth_fail_expired_v2")
    items = {h: (w, ids) for h, w, ids in outline.build(replay_bundle(cfg, tx, "eth_fail_expired_v2"), "support")}
    what, ids = items["What to do"]
    pattern = next(e.id for e in replay_bundle(cfg, tx, "eth_fail_expired_v2").items
                   if e.kind == "timeline" and e.data.get("pattern") == "retried_ok")
    assert "already succeeded later" in what and ids == [pattern]
