"""PHASE3 T3 (R3, R4, D47): follow-up questions with read-only tools, on real recorded data."""
import json
import re

import httpx
import pytest

from anychain.chat import MAX_QUESTIONS, MAX_TOOLS, ChatSession, Tools, parse_tool_request
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.rpc import RpcClient
from anychain.config import load_config
from tests.conftest import ROOT, make_transport, replay_bundle
from tests.test_golden import _case

CELO = "celo-mainnet"
SENDER, TOKEN = "0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"
RECIPIENT = "0x14634De7D71618013Dc48F2e85E80A28023f4367"  # its balance read recorded from the node on 2026-10-08


def _session(fixture="celo_fail_balance_confirmed", config=CELO, others=()):
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    _c, tx = _case(fixture)
    client = httpx.Client(transport=make_transport(fixture))
    by_hash = {_case(f)[1].lower(): f for f in others}
    tools = Tools(cfg, rpc_factory=lambda: RpcClient(cfg.rpc, client),
                  explorer_factory=lambda: ExplorerClient(cfg.explorer, client),
                  build=lambda h: replay_bundle(cfg, h, by_hash[h.lower()]))
    return ChatSession(cfg, replay_bundle(cfg, tx, fixture), "developer", tools)


class Scripted:
    """A model that answers from a script; each step sees the prompt it was sent."""

    def __init__(self, *steps):
        self.steps, self.prompts, self.last_usage = list(steps), [], None

    def complete(self, system, user):
        self.prompts.append((system, user))
        step = self.steps.pop(0)
        return step(user) if callable(step) else step


def _fact_with(user: str, needle: str) -> str:
    evidence = json.loads(user.split("\n\n")[0])["evidence"]
    return next(e["id"] for e in evidence if needle in e["fact"])


def test_a_read_asked_in_the_chat_becomes_a_cited_fact():
    # R3's acceptance on the real Celo failure (block 78962884): a balance the evidence does not hold
    session = _session()
    ask = json.dumps({"tools": [{"tool": "read", "contract": TOKEN, "function": "balanceOf(address)",
                                 "args": [RECIPIENT], "returns": "uint256", "block": "parent"}]})
    model = Scripted(ask, lambda user: f"Before it, the recipient held 33540 [{_fact_with(user, 'Asked in the chat')}].")
    turn = session.ask("What did the recipient hold before?", model)
    assert turn.outcome == "ok" and len(turn.new_facts) == 1
    [fact] = [e for e in session.bundle.items if e.id == turn.new_facts[0]]
    assert fact.kind == "chat_read" and "The node returned 33540" in fact.text and "block 78962883" in fact.text
    assert fact.confidence == "confirmed" and fact.sources[0].kind == "rpc"
    assert f"[{fact.id}]" in turn.answer
    assert "balanceOf" in model.prompts[0][0] and '"tools"' in model.prompts[0][0]  # the protocol is in the prompt


@pytest.mark.parametrize("request_, reason", [
    ({"tool": "write", "contract": TOKEN}, "unknown tool"),
    ({"tool": "read", "contract": "0x123", "function": "balanceOf(address)", "args": [SENDER], "returns": "uint256"},
     "0x address"),
    ({"tool": "read", "contract": TOKEN, "function": "balanceOf(address)", "args": [], "returns": "uint256"},
     "takes 1 argument"),
    ({"tool": "read", "contract": TOKEN, "function": "f((uint256,address))", "args": [[1, SENDER]],
      "returns": "uint256"}, "simple types"),
    ({"tool": "read", "contract": TOKEN, "function": "balanceOf(address)", "args": [SENDER], "returns": "uint256",
      "block": 99999999999}, "block must be"),
    ({"tool": "read", "contract": TOKEN, "function": "balanceOf(address)", "args": ["not an address"],
      "returns": "uint256"}, "0x address"),
    ({"tool": "transaction", "hash": "0x12"}, "64 hex"),
    ({"tool": "code", "contract": TOKEN, "function": "do; rm -rf"}, "must be a name"),
])
def test_bad_requests_are_refused_with_the_reason_and_add_nothing(request_, reason):
    session = _session()
    before = len(session.bundle.items)
    model = Scripted(json.dumps({"tools": [request_]}), "I could not get that data.")
    turn = session.ask("Something?", model)
    assert len(session.bundle.items) == before and turn.new_facts == []
    assert reason in turn.tool_calls[0]["result"]
    assert "refused" in model.prompts[1][1]  # the reason is told back to the model


def test_the_code_tool_gives_a_function_of_the_verified_source():
    session = _session()
    ask = json.dumps({"tools": [{"tool": "code", "contract": TOKEN, "function": "_transfer"}]})
    turn = session.ask("Where does it check the balance?", Scripted(ask, "It is in _transfer [E1]."))
    [fact] = [e for e in session.bundle.items if e.id in turn.new_facts]
    assert fact.kind == "code" and "function _transfer(" in fact.text and "236 |" in fact.text


def test_the_transaction_tool_adds_another_transactions_summary():
    other = "celo_fee_currency"
    session = _session(others=(other,))
    ask = json.dumps({"tools": [{"tool": "transaction", "hash": _case(other)[1]}]})
    turn = session.ask("And this other one?", Scripted(ask, "It is in the evidence [E1]."))
    kinds = {e.kind for e in session.bundle.items if e.id in turn.new_facts}
    assert kinds == {"related_tx"} and turn.outcome == "ok"


def test_at_most_three_tool_requests_per_question():
    session = _session()
    read = {"tool": "read", "contract": TOKEN, "function": "balanceOf(address)", "args": [SENDER],
            "returns": "uint256"}
    turn = session.ask("Many reads?", Scripted(json.dumps({"tools": [read] * (MAX_TOOLS + 2)}), "Done [E1]."))
    refused = [c for c in turn.tool_calls if str(c["result"]).startswith("refused: at most")]
    assert len(turn.tool_calls) == MAX_TOOLS + 2 and len(refused) == 2


def test_an_invented_value_is_retried_then_withheld():
    session = _session()
    turn = session.ask("How much?", Scripted("It held 999999 [E1].", "It held 888888 [E1]."))
    assert turn.outcome == "withheld" and turn.answer is None and turn.problems
    turn = session.ask("How much?", Scripted("It held 999999 [E1].", "It failed [E1]."))
    assert turn.outcome == "retried" and turn.answer == "It failed [E1]."


def test_a_model_that_keeps_asking_for_tools_must_answer():
    session = _session()
    read = json.dumps({"tools": [{"tool": "read", "contract": TOKEN, "function": "decimals()", "args": [],
                                  "returns": "uint8"}]})
    turn = session.ask("Decimals?", Scripted(read, read, read, "It failed [E1]."))
    assert turn.outcome == "ok" and turn.answer == "It failed [E1]."  # asked to answer: not a failed check


def test_a_session_ends_after_its_question_limit():
    session = _session()
    for _ in range(MAX_QUESTIONS):
        session.ask("Status?", Scripted("It failed [E1]."))
    turn = session.ask("Again?", Scripted("It failed [E1]."))
    assert turn.outcome == "refused" and "start a new one" in turn.error


def test_tool_requests_are_parsed_only_from_a_whole_json_reply():
    assert parse_tool_request('{"tools": [{"tool": "code"}]}') == [{"tool": "code"}]
    assert parse_tool_request('```json\n{"tools": []}\n```') == []
    assert parse_tool_request('The answer mentions {"tools": []} in passing [E1].') is None
    assert parse_tool_request("It failed [E1].") is None


# ---- the API and the command line ----

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from anychain import cli  # noqa: E402
from anychain.api import create_app  # noqa: E402
from anychain.cache import BundleCache  # noqa: E402


def _app(event_log, tmp_path, model):
    session = _session()
    cfg = session.cfg
    return TestClient(create_app(cfg, log=event_log, store=BundleCache(tmp_path / "c.db"),
                                 build=lambda h, c: replay_bundle(c, h, "celo_fail_balance_confirmed"),
                                 finality=lambda: None, backend_factory=lambda: model, tools=session.tools),
                      base_url="http://127.0.0.1:8000")


def test_chat_over_the_api_starts_continues_and_logs(event_log, tmp_path):
    _c, tx = _case("celo_fail_balance_confirmed")
    ask = json.dumps({"tools": [{"tool": "read", "contract": TOKEN, "function": "balanceOf(address)",
                                 "args": [RECIPIENT], "returns": "uint256"}]})
    model = Scripted(ask, lambda user: f"It held 33540 [{_fact_with(user, 'Asked in the chat')}].", "It failed [E1].")
    client = _app(event_log, tmp_path, model)
    first = client.post("/chat", json={"hash": tx, "message": "Balance before?", "mode": "developer"}).json()
    assert first["outcome"] == "ok" and first["new_facts"][0]["kind"] == "chat_read"
    assert "returned 33540" in first["new_facts"][0]["text"] and first["questions_left"] == MAX_QUESTIONS - 1
    second = client.post("/chat", json={"session_id": first["session_id"], "message": "And the status?"}).json()
    assert second["answer"] == "It failed [E1]." and second["session_id"] == first["session_id"]
    rows = event_log.recent(2)
    assert {r["source"] for r in rows} == {"chat"} and {r["writer"] for r in rows} == {"ok"}
    assert client.post("/feedback", json={"run_id": second["run_id"], "value": "up"}).json() == {"saved": True}


def test_chat_over_the_api_refuses_unknown_sessions_and_empty_starts(event_log, tmp_path):
    client = _app(event_log, tmp_path, Scripted())
    assert client.post("/chat", json={"session_id": "nope", "message": "hi"}).status_code == 404
    assert client.post("/chat", json={"message": "hi"}).status_code == 422
    assert client.post("/chat", json={"hash": "0x12", "message": "hi"}).status_code == 400


def test_chat_on_the_command_line(monkeypatch, event_log):
    _c, tx = _case("celo_fail_balance_confirmed")
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, "celo_fail_balance_confirmed"))
    monkeypatch.setattr("anychain.writer.backend_for", lambda llm: Scripted("It failed [E1].", "It held 999999 [E1].",
                                                                             "Still 888888 [E1]."))
    out = CliRunner().invoke(cli.app, ["chat", tx, "--config", str(ROOT / "configs" / "celo-mainnet.yaml")],
                             input="What happened?\nHow much?\n\n").output
    assert "It failed [E1]." in out and "withheld" in out
    assert [r["writer"] for r in event_log.recent(2)] == ["withheld", "ok"]


# ---- review of T3 ----

def _read(fn="balanceOf(address)", args=(SENDER,), returns="uint256", **extra):
    return {"tool": "read", "contract": TOKEN, "function": fn, "args": list(args), "returns": returns, **extra}


@pytest.mark.parametrize("request_", [
    _read("f(uint8)", [300], "uint256"), _read("f(uint256)", [2**256], "uint256"), _read("f(int8)", [-129], "uint256"),
    _read("f(uint256)", [1.9], "uint256"), _read("f(uint256)", [True], "uint256"), _read("f(,address)", [1, SENDER]),
])
def test_values_outside_their_type_are_refused_not_a_crash(request_):
    session = _session()
    turn = session.ask("x", Scripted(json.dumps({"tools": [request_]}), "It failed [E1]."))
    assert turn.outcome == "ok" and str(turn.tool_calls[0]["result"]).startswith("refused")


def test_an_unexpected_tool_failure_is_a_refusal(monkeypatch):
    session = _session()
    monkeypatch.setattr(session.tools, "_code", lambda *a: (_ for _ in ()).throw(KeyError("odd metadata")))
    turn = session.ask("x", Scripted(json.dumps({"tools": [{"tool": "code", "contract": TOKEN, "function": "f"}]}),
                                     "It failed [E1]."))
    assert "refused: the tool failed unexpectedly" in turn.tool_calls[0]["result"] and turn.outcome == "ok"


def test_the_node_url_never_reaches_the_model_or_the_reader():
    from anychain.chat import ChatSession, Tools
    cfg = load_config(str(ROOT / "configs" / f"{CELO}.yaml"))
    _c, tx = _case("celo_fail_balance_confirmed")
    host = httpx.URL(cfg.rpc.url).host
    offline = httpx.Client(transport=make_transport("celo_fail_balance_confirmed", {host}))
    tools = Tools(cfg, rpc_factory=lambda: RpcClient(cfg.rpc, offline),
                  explorer_factory=lambda: ExplorerClient(cfg.explorer, offline), build=lambda h: None)
    session = ChatSession(cfg, replay_bundle(cfg, tx, "celo_fail_balance_confirmed"), "developer", tools)
    model = Scripted(json.dumps({"tools": [_read()]}), "It failed [E1].")
    turn = session.ask("x", model)
    assert host not in json.dumps(turn.tool_calls) and host not in model.prompts[1][1]


def test_a_number_the_model_put_in_a_tool_request_is_not_evidence():
    session = _session()
    laundering = _read("balanceOfAt(address,uint256)", [SENDER, 987654])
    turn = session.ask("x", Scripted(json.dumps({"tools": [laundering]}), "The sender held 987654 [E1].",
                                     "The sender held 987654 [E1]."))
    assert turn.outcome == "withheld"  # the read may fail or succeed: the 987654 it was asked with proves nothing


def test_a_read_says_the_type_was_chosen_in_the_chat_and_shows_the_raw_word():
    session = _session()
    turn = session.ask("x", Scripted(json.dumps({"tools": [_read(args=(RECIPIENT,), returns="address")]}),
                                     "It failed [E1]."))
    [fact] = [e for e in session.bundle.items if e.id in turn.new_facts]
    assert "decoded as address, the type the chat asked for (not read from the contract's ABI)" in fact.text
    assert "0x0000000000000000000000000000000000000000000000000000000000008304" in fact.text  # 33540, raw


def test_the_same_read_is_not_asked_twice():
    session = _session()
    turn = session.ask("x", Scripted(json.dumps({"tools": [_read(), _read()]}), "It failed [E1]."))
    assert turn.new_facts == [] and turn.tool_calls[0]["result"] == turn.tool_calls[1]["result"]  # the diagnosis's E7
    assert len(set(turn.tool_calls[0]["result"])) == 1


@pytest.mark.parametrize("reply", [
    "I need one read.\n```json\n" + json.dumps({"tools": [_read(returns="uint8")]}) + "\n```",
    json.dumps(_read(returns="uint8")),  # one request without the wrapper
])
def test_a_tool_request_inside_prose_or_without_the_wrapper_is_still_a_request(reply):
    session = _session()
    turn = session.ask("x", Scripted(reply, "It failed [E1]."))
    assert turn.tool_calls and turn.answer == "It failed [E1]."


def test_asking_for_tools_again_is_not_a_failed_check():
    session = _session()
    read = json.dumps({"tools": [_read("decimals()", [], "uint8")]})
    turn = session.ask("x", Scripted(read, read, read, "It failed [E1]."))
    assert turn.outcome == "ok" and turn.problems == []


def test_a_file_from_a_configured_repo():
    from anychain.chat import ChatSession, Tools
    from tests.test_repos import SET_PAUSER
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    client = httpx.Client(transport=make_transport("eth_brlc_set_pauser"))
    tools = Tools(cfg, rpc_factory=lambda: RpcClient(cfg.rpc, client),
                  explorer_factory=lambda: ExplorerClient(cfg.explorer, client), build=lambda h: None)
    session = ChatSession(cfg, replay_bundle(cfg, SET_PAUSER, "eth_brlc_set_pauser"), "developer", tools)
    repo = "https://github.com/cloudwallk/brlc-token"
    ok = {"tool": "file", "repo": repo, "path": "contracts/base/common/PausableExtUpgradeable.sol", "lines": [100, 115]}
    bad = [{"tool": "file", "repo": repo, "path": "../../etc/passwd"},
           {"tool": "file", "repo": "https://github.com/someone/else", "path": "a.sol"},
           {"tool": "file", "repo": repo, "path": "README.md"}]
    turn = session.ask("x", Scripted(json.dumps({"tools": [ok] + bad[:2]}), "It is in the repo [E1]."))
    [fact] = [e for e in session.bundle.items if e.id in turn.new_facts]
    assert fact.kind == "code" and "103 |" in fact.text and "setPauser" in fact.text
    assert fact.sources[0].url.startswith("https://github.com/cloudwallk/brlc-token/blob/74a5498")
    assert all(str(c["result"]).startswith("refused") for c in turn.tool_calls[1:])
    turn = session.ask("y", Scripted(json.dumps({"tools": [bad[2]]}), "It failed [E1]."))
    assert str(turn.tool_calls[0]["result"]).startswith("refused")


def test_two_questions_at_once_on_one_session_do_not_mix():
    import threading
    session = _session()
    model = Scripted(*[json.dumps({"tools": [_read("decimals()", [], "uint8")]}), "It failed [E1]."] * 2)
    threads = [threading.Thread(target=session.ask, args=("x", model)) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [e.id for e in session.bundle.items]
    assert len(ids) == len(set(ids))


def test_a_continued_conversation_keeps_its_mode(event_log, tmp_path):
    _c, tx = _case("celo_fail_balance_confirmed")
    client = _app(event_log, tmp_path, Scripted("It failed [E1].", "It failed [E1]."))
    first = client.post("/chat", json={"hash": tx, "message": "x", "mode": "support"}).json()
    other = client.post("/chat", json={"session_id": first["session_id"], "message": "y", "mode": "auditor"})
    assert other.status_code == 422 and "mode" in other.json()["detail"]
