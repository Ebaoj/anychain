"""The model a reader picks, with its API key, saved from the CLI or the page (D61). The key is a test value:
nothing here calls a provider."""
import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from anychain import cli, llm_settings
from anychain.api import create_app
from anychain.config import load_config
from anychain.writer import AnthropicBackend, OpenAIBackend, backend_for
from tests.conftest import ROOT
from tests.test_api import LOCAL, make_transport

ETH = str(ROOT / "configs" / "ethereum-mainnet.yaml")
KEY = "sk-test-not-a-real-key-0000a1b2"
# The providers' list shapes as their references give them (D61); the ids are test values, not real models.
ANTHROPIC_LIST = {"data": [
    {"type": "model", "id": "claude-test-new", "display_name": "Claude Test New", "lifecycle": "active"},
    {"type": "model", "id": "claude-test-old", "display_name": "Claude Test Old", "lifecycle": "deprecated"},
    {"type": "model", "id": "claude-test-gone", "display_name": "Claude Test Gone", "lifecycle": "retired"}],
    "has_more": False}
OPENAI_LIST = {"object": "list", "data": [
    {"id": "gpt-test-chat", "object": "model", "created": 200, "owned_by": "openai"},
    {"id": "gpt-test-older", "object": "model", "created": 100, "owned_by": "openai"},
    {"id": "text-embedding-test", "object": "model", "created": 300, "owned_by": "openai"},
    {"id": "whisper-test", "object": "model", "created": 300, "owned_by": "openai"}]}


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    """The providers' model lists, answered locally; `seen` records the auth headers sent."""
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if request.headers.get("x-api-key") == "bad" or request.headers.get("authorization") == "Bearer bad":
            return httpx.Response(401, json={"error": {"message": "invalid key bad"}})
        return httpx.Response(200, json=ANTHROPIC_LIST if "anthropic" in request.url.host else OPENAI_LIST)
    monkeypatch.setattr(llm_settings, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_saving_writes_a_private_file_and_never_shows_the_key(tmp_path):
    llm_settings.save("anthropic", "claude-test-new", KEY)
    path = llm_settings.path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    shown = llm_settings.describe()
    assert shown["provider"] == "anthropic" and shown["keys"]["anthropic"] == "saved, ends in a1b2"
    assert KEY not in json.dumps(shown)


def test_the_saved_choice_overrides_the_config():
    cfg = load_config(ETH)
    assert cfg.llm.provider == "claude_code"
    llm_settings.save("openai", "gpt-test-chat", KEY)
    llm = llm_settings.effective(cfg.llm)
    assert (llm.provider, llm.model) == ("openai", "gpt-test-chat")
    assert isinstance(backend_for(cfg.llm), OpenAIBackend)


def test_the_saved_key_reaches_the_backend(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm_settings.save("openai", "gpt-test-chat", KEY)
    sent = {}

    def handler(request):
        sent["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {}})
    cfg = load_config(ETH)
    backend = OpenAIBackend(llm_settings.effective(cfg.llm), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert backend.complete("s", "u") == "OK" and sent["auth"] == f"Bearer {KEY}"


def test_an_anthropic_key_is_passed_to_the_sdk(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm_settings.save("anthropic", "claude-test-new", KEY)
    made = {}

    class Fake:
        def __init__(self, **kw):
            made.update(kw)
            raise RuntimeError("stop here")
    monkeypatch.setattr("anthropic.Anthropic", Fake)
    try:
        AnthropicBackend(llm_settings.effective(load_config(ETH).llm)).complete("s", "u")
    except Exception:
        pass
    assert made.get("api_key") == KEY


def test_the_cli_asks_for_the_key_hidden_and_never_prints_it():
    out = CliRunner().invoke(cli.app, ["llm", "set", "--provider", "anthropic"], input=KEY + "\n1\n")
    assert out.exit_code == 0 and KEY not in out.output and "a1b2" in out.output
    assert "1. Claude Test New" in out.output and "Claude Test Gone" not in out.output
    assert llm_settings.describe()["model"] == "claude-test-new"
    shown = CliRunner().invoke(cli.app, ["llm", "show"])
    assert "anthropic" in shown.output and KEY not in shown.output
    CliRunner().invoke(cli.app, ["llm", "clear"])
    assert llm_settings.describe()["provider"] is None


def _client(event_log):
    cfg = load_config(ETH)
    app = create_app(cfg, log=event_log, store=None, finality=lambda: None,
                     probe_client=httpx.Client(transport=make_transport("health_ethereum")))
    return TestClient(app, base_url=LOCAL)


def test_the_page_saves_the_key_and_gets_back_only_its_end(event_log):
    c = _client(event_log)
    r = c.post("/settings/llm", json={"provider": "anthropic", "model": "claude-test-new", "api_key": KEY},
               headers={"X-AnyChain-Request": "1"})
    assert r.status_code == 200 and KEY not in r.text and "a1b2" in r.text
    assert KEY not in c.get("/settings/llm").text
    health = c.get("/health").json()["sources"]["model"]
    assert health["provider"] == "anthropic" and health["status"] == "configured"


def test_another_site_cannot_save_a_key(event_log):
    c = _client(event_log)
    body = {"provider": "anthropic", "api_key": KEY}
    assert c.post("/settings/llm", json=body).status_code == 403  # no custom header: a plain cross-site form/fetch
    assert c.post("/settings/llm", json=body, headers={"X-AnyChain-Request": "1",
                                                       "Origin": "https://evil.example"}).status_code == 403
    assert llm_settings.describe()["provider"] is None


def test_the_models_come_from_the_providers_list(providers):
    anthropic = llm_settings.list_models("anthropic", KEY)
    assert [m["id"] for m in anthropic] == ["claude-test-new", "claude-test-old"]  # retired left out
    assert anthropic[1]["label"].endswith("(deprecated)")
    assert providers[-1]["x-api-key"] == KEY and providers[-1]["anthropic-version"] == "2023-06-01"
    openai = llm_settings.list_models("openai", KEY)
    assert [m["id"] for m in openai] == ["gpt-test-chat", "gpt-test-older"]  # no embeddings or audio, newest first


def test_a_model_not_in_the_list_is_refused():
    with pytest.raises(ValueError, match="not in the models"):
        llm_settings.save("openai", "gpt-typo", KEY)
    assert llm_settings.describe()["provider"] is None


def test_a_refused_key_says_so_without_echoing_the_providers_text():
    with pytest.raises(llm_settings.ModelListError) as exc:
        llm_settings.list_models("anthropic", "bad")
    assert "refused the key (HTTP 401)" in str(exc.value) and "invalid key" not in str(exc.value)


def test_the_page_lists_the_models_for_a_pasted_key(event_log):
    c = _client(event_log)
    r = c.post("/settings/llm/models", json={"provider": "openai", "api_key": KEY}, headers={"X-AnyChain-Request": "1"})
    assert r.status_code == 200 and [m["id"] for m in r.json()["models"]] == ["gpt-test-chat", "gpt-test-older"]
    assert KEY not in r.text
    assert c.post("/settings/llm/models", json={"provider": "openai", "api_key": KEY}).status_code == 403
    assert llm_settings.describe()["provider"] is None  # listing saves nothing


def test_an_openai_choice_needs_a_model(event_log):
    r = _client(event_log).post("/settings/llm", json={"provider": "openai", "api_key": KEY},
                                headers={"X-AnyChain-Request": "1"})
    assert r.status_code == 422


def test_the_list_marks_the_recommended_minimum_and_nano(monkeypatch):
    """D77: the model list says which OpenAI model is the recommended minimum (gpt-4.1-mini) and that gpt-4.1-nano
    is not recommended (it misread a fact on open questions, D76); the page and `llm set` show these labels."""
    listed = {"data": [{"id": "gpt-4.1-nano", "created": 1}, {"id": "gpt-4.1-mini", "created": 2}]}
    monkeypatch.setattr(llm_settings, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=listed))))
    labels = {m["id"]: m["label"] for m in llm_settings.list_models("openai", KEY)}
    assert "recommended minimum" in labels["gpt-4.1-mini"] and "not recommended" in labels["gpt-4.1-nano"]
