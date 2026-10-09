"""The model a reader picks, with its API key, saved from the CLI or the page (D61). The key is a test value:
nothing here calls a provider."""
import json
import os
import stat

import httpx
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


def test_saving_writes_a_private_file_and_never_shows_the_key(tmp_path):
    llm_settings.save("anthropic", "claude-sonnet-5-5", KEY)
    path = llm_settings.path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    shown = llm_settings.describe()
    assert shown["provider"] == "anthropic" and shown["keys"]["anthropic"] == "saved, ends in a1b2"
    assert KEY not in json.dumps(shown)


def test_the_saved_choice_overrides_the_config():
    cfg = load_config(ETH)
    assert cfg.llm.provider == "claude_code"
    llm_settings.save("openai", "gpt-test-model", KEY)
    llm = llm_settings.effective(cfg.llm)
    assert (llm.provider, llm.model) == ("openai", "gpt-test-model")
    assert isinstance(backend_for(cfg.llm), OpenAIBackend)


def test_the_saved_key_reaches_the_backend(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm_settings.save("openai", "gpt-test-model", KEY)
    sent = {}

    def handler(request):
        sent["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {}})
    cfg = load_config(ETH)
    backend = OpenAIBackend(llm_settings.effective(cfg.llm), client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert backend.complete("s", "u") == "OK" and sent["auth"] == f"Bearer {KEY}"


def test_an_anthropic_key_is_passed_to_the_sdk(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm_settings.save("anthropic", "claude-sonnet-5-5", KEY)
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
    out = CliRunner().invoke(cli.app, ["llm", "set", "--provider", "anthropic"], input=KEY + "\n")
    assert out.exit_code == 0 and KEY not in out.output and "a1b2" in out.output
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
    r = c.post("/settings/llm", json={"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key": KEY},
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


def test_an_openai_choice_needs_a_model(event_log):
    r = _client(event_log).post("/settings/llm", json={"provider": "openai", "api_key": KEY},
                                headers={"X-AnyChain-Request": "1"})
    assert r.status_code == 422
