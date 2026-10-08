"""PHASE3 T2 (R1, R5, D46): the local API answers as the command line does, from recorded real traffic."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from anychain import cli
from anychain.api import create_app
from anychain.cache import BundleCache
from anychain.config import load_config
from anychain.collectors.replay import ReplayTransport
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

ETH = str(ROOT / "configs" / "ethereum-mainnet.yaml")
LOCAL = "http://127.0.0.1:8000"  # the API answers only requests addressed to this machine (DNS rebinding)


def make_transport(name, offline_hosts=None):
    """The real /health probe answers, recorded with scripts/record_health.py on 2026-10-08."""
    return ReplayTransport(ROOT / "tests" / "fixtures_health" / f"{name}.json", offline_hosts)


def _client(event_log, tmp_path, fixture="eth_usdc_transfer", health="health_ethereum", **kw):
    cfg = load_config(ETH)
    app = create_app(cfg, log=event_log, store=BundleCache(tmp_path / "api_cache.db"),
                     build=lambda h, c: replay_bundle(c, h, fixture), finality=lambda: None,
                     probe_client=httpx.Client(transport=make_transport(health, **kw)), **kw.pop("app", {}))
    return TestClient(app, base_url=LOCAL)


def test_health_reports_the_network_and_each_source(event_log, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _c: "/usr/local/bin/claude")
    body = _client(event_log, tmp_path).get("/health").json()
    assert body["network"] == {"name": "ethereum-mainnet", "chain_id": 1, "explorer": "https://eth.blockscout.com"}
    assert body["sources"]["explorer"]["status"] == "ok" and body["sources"]["node"]["status"] == "ok"
    assert body["sources"]["model"]["status"] == "installed" and body["status"] == "ok"


def test_health_says_what_is_down(event_log, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _c: None)
    body = _client(event_log, tmp_path, offline_hosts={"eth.blockscout.com"}).get("/health").json()
    assert body["sources"]["explorer"]["status"] == "down" and body["sources"]["node"]["status"] == "ok"
    assert body["sources"]["model"]["status"] == "missing" and body["status"] == "degraded"


def test_health_catches_a_node_on_another_chain(event_log, tmp_path):
    cfg = load_config(ETH)
    cfg.network.chain_id = 30  # the Ethereum node, under a config that says Rootstock's chain
    app = create_app(cfg, log=event_log, store=None, probe_client=httpx.Client(transport=make_transport("health_ethereum")))
    assert TestClient(app, base_url=LOCAL).get("/health").json()["sources"]["node"]["status"] == "wrong_chain"


def test_explain_returns_what_the_command_line_prints(event_log, tmp_path, monkeypatch):
    _c, tx = _case("eth_usdc_transfer")
    api = _client(event_log, tmp_path).post("/explain", json={"hash": tx, "mode": "support", "write": False}).json()
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "eth_usdc_transfer"))
    out = CliRunner().invoke(cli.app, ["explain", tx, "--config", ETH, "--json", "--no-llm", "--mode", "support"])
    expected = json.loads(out.stdout)
    assert {k: v for k, v in api.items() if k != "run_id"} == expected
    assert isinstance(api["run_id"], int)
    row = [r for r in event_log.recent(5) if r["id"] == api["run_id"]][0]
    assert row["source"] == "api" and row["mode"] == "support"


@pytest.mark.parametrize("mode", ["support", "developer", "auditor"])
def test_explain_with_the_model(event_log, tmp_path, mode):
    from anychain.writer import CheckedAnswer
    _c, tx = _case("eth_usdc_transfer")
    cfg = load_config(ETH)
    app = create_app(cfg, log=event_log, store=BundleCache(tmp_path / "c.db"),
                     build=lambda h, c: replay_bundle(c, h, "eth_usdc_transfer"), finality=lambda: None,
                     write_fn=lambda b, c, mode: CheckedAnswer(f"Written for {mode} [E1].", "ok", []))
    body = TestClient(app, base_url=LOCAL).post("/explain", json={"hash": tx, "mode": mode}).json()
    assert body["summary"] == f"Written for {mode} [E1]." and body["summary_status"] == "ok"
    assert body["mode"] == mode  # each mode reaches the writer (R5)


@pytest.mark.parametrize("payload, status", [
    ({"hash": "0x123"}, 400),  # not a hash
    ({"hash": "0x" + "ab" * 32, "mode": "boss"}, 422),  # not a mode
    ({"mode": "support"}, 422),  # no hash
    ({"hash": "0x" + "ab" * 32, "Mode": "auditor"}, 422),  # a misspelled field is refused, not dropped
])
def test_explain_refuses_bad_requests_clearly(event_log, tmp_path, payload, status):
    response = _client(event_log, tmp_path).post("/explain", json=payload)
    assert response.status_code == status and "detail" in response.json()


def test_an_unexpected_error_is_a_logged_500_not_a_traceback(event_log, tmp_path):
    cfg = load_config(ETH)

    def broken(h, c):
        raise RuntimeError("something odd in the data")
    app = create_app(cfg, log=event_log, store=None, build=broken, finality=lambda: None)
    response = TestClient(app, base_url=LOCAL, raise_server_exceptions=False).post("/explain", json={"hash": "0x" + "ab" * 32})
    assert response.status_code == 500 and "something odd" in response.json()["detail"]
    assert "Traceback" not in response.text
    assert event_log.recent(1)[0]["outcome"] == "crash"


def test_feedback_is_saved_on_the_answer(event_log, tmp_path):
    _c, tx = _case("eth_usdc_transfer")
    client = _client(event_log, tmp_path)
    run_id = client.post("/explain", json={"hash": tx, "write": False}).json()["run_id"]
    assert client.post("/feedback", json={"run_id": run_id, "value": "up"}).json() == {"saved": True}
    assert [r for r in event_log.recent(5) if r["id"] == run_id][0]["feedback"] == "up"
    assert client.post("/feedback", json={"run_id": 999999, "value": "up"}).status_code == 404
    assert client.post("/feedback", json={"run_id": run_id, "value": "meh"}).status_code == 422


def test_serve_refuses_a_host_other_machines_can_reach(monkeypatch):
    started = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **kw: started.append((host, port)))
    bad = CliRunner().invoke(cli.app, ["serve", "--config", ETH, "--host", "0.0.0.0"])
    assert bad.exit_code != 0 and "127.0.0.1" in bad.output and started == []
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    ok = CliRunner().invoke(cli.app, ["serve", "--config", ETH, "--port", str(free)])
    assert ok.exit_code == 0 and started == [("127.0.0.1", free)]


def test_serve_says_when_the_port_is_taken(monkeypatch):
    import socket
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = CliRunner().invoke(cli.app, ["serve", "--config", ETH, "--port", str(port)])
    assert result.exit_code != 0 and "another program is listening" in result.output


def test_a_request_addressed_to_another_name_is_refused(event_log, tmp_path):
    # DNS rebinding: a web page whose name resolves to 127.0.0.1 must not reach the API
    client = _client(event_log, tmp_path)
    assert client.get("/health", headers={"host": "evil.example:8000"}).status_code == 400
    assert client.get("/health", headers={"host": "localhost:8000"}).status_code == 200


def test_error_details_never_show_endpoint_urls(event_log, tmp_path):
    from anychain.collectors.http import CollectorError
    cfg = load_config(ETH)

    def broken(h, c):
        raise CollectorError("https://mainnet.infura.io/v3/SECRETKEY failed after 3 attempts (ReadTimeout)",
                             retryable=True)
    app = create_app(cfg, log=event_log, store=None, build=broken, finality=lambda: None,
                     probe_client=httpx.Client(transport=make_transport("health_ethereum",
                                                                        {"ethereum-rpc.publicnode.com"})))
    client = TestClient(app, base_url=LOCAL, raise_server_exceptions=False)
    assert "SECRETKEY" not in client.post("/explain", json={"hash": "0x" + "ab" * 32}).text
    assert "publicnode" not in client.get("/health").text  # the node's URL is not in the probe's detail


def test_feedback_only_on_answers_the_api_gave(event_log, tmp_path):
    from anychain.events import RunEvent
    other = event_log.record(RunEvent(network="n", tx_hash="0x1", source="batch", outcome="ok", duration_ms=1))
    crash = event_log.record(RunEvent(network="n", tx_hash="0x2", source="api", outcome="crash", duration_ms=1))
    client = _client(event_log, tmp_path)
    for run_id in (other, crash):
        assert client.post("/feedback", json={"run_id": run_id, "value": "down"}).status_code == 404


def test_feedback_with_the_log_off_says_so(tmp_path):
    from anychain.events import NullEventLog
    app = create_app(load_config(ETH), log=NullEventLog(), store=None)
    response = TestClient(app, base_url=LOCAL).post("/feedback", json={"run_id": 1, "value": "up"})
    assert response.status_code == 503 and "off" in response.json()["detail"]


def test_a_failure_after_collecting_is_logged_too(event_log, tmp_path):
    cfg = load_config(ETH)
    _c, tx = _case("eth_usdc_transfer")

    def broken_writer(b, c, mode):
        raise RuntimeError("an unexpected failure while writing")
    app = create_app(cfg, log=event_log, store=None, build=lambda h, c: replay_bundle(c, h, "eth_usdc_transfer"),
                     finality=lambda: None, write_fn=broken_writer)
    response = TestClient(app, base_url=LOCAL, raise_server_exceptions=False).post("/explain", json={"hash": tx})
    assert response.status_code == 500 and event_log.recent(1)[0]["outcome"] == "crash"
