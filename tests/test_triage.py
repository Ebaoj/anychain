"""PHASE4 T1 (R1, plan 4.5): at most one clarifying question, chosen by code, only when the answer changes what the
tool can say. Every case runs on real recordings."""
import httpx
from fastapi.testclient import TestClient

from anychain.api import create_app
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.replay import ReplayTransport
from anychain.config import load_config
from anychain.service import WRITE, answer_transaction
from anychain.triage import apply_answer, for_bundle, for_input
from anychain.writer import write_checked
from tests.conftest import ROOT, replay_bundle
from tests.test_api import LOCAL, make_transport
from tests.test_golden import _case

ETH = str(ROOT / "configs" / "ethereum-mainnet.yaml")
SENDER = "0xCA62C34d54b445283121905F19193416212117c4"  # eth_usdc_transfer's sender; its list recorded 2026-10-08
USDC_TX = "0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952"


def _explorer(cfg):
    transport = ReplayTransport(ROOT / "tests" / "fixtures_triage" / "eth_address_recent.json")
    return ExplorerClient(cfg.explorer, httpx.Client(transport=transport))


def _bundle(fixture, config="ethereum-mainnet"):
    cfg = load_config(str(ROOT / "configs" / f"{config}.yaml"))
    _c, tx = _case(fixture)
    return cfg, replay_bundle(cfg, tx, fixture)


class Backend:
    last_usage = None

    def __init__(self):
        self.users = []

    def complete(self, system, user):
        self.users.append(user)
        return "The transaction succeeded [E1]."


# ---- case 1: the input is not a transaction hash ----

def test_an_address_gets_its_latest_transactions_to_pick_from():
    cfg = load_config(ETH)
    c = for_input(SENDER, _explorer(cfg))
    assert c.kind == "which_transaction" and "is an address" in c.question
    assert [o.id for o in c.options][0] == USDC_TX and "transfer" in c.options[0].label


def test_text_that_is_neither_a_hash_nor_an_address_asks_for_the_hash():
    c = for_input("my payment from yesterday", None)
    assert c.kind == "which_transaction" and c.options == [] and c.free_text


def test_a_hash_needs_no_question():
    assert for_input(USDC_TX, None) is None


def test_an_address_whose_list_cannot_be_read_asks_for_the_hash():
    cfg = load_config(ETH)
    down = ExplorerClient(cfg.explorer, httpx.Client(transport=ReplayTransport(
        ROOT / "tests" / "fixtures_triage" / "eth_address_recent.json", {"eth.blockscout.com"})))
    c = for_input(SENDER, down)
    assert c.options == [] and "could not be read" in c.question


# ---- case 2: it succeeded, but the reader did not receive it ----

def test_not_received_on_a_success_asks_which_payment():
    cfg, b = _bundle("eth_usdc_transfer")
    c = for_bundle(b, "não recebi o pagamento", cfg)
    assert c.kind == "expected_receipt" and c.options and c.free_text
    transfer = next(e for e in b.items if e.kind == "token_transfer")
    assert c.options[0].id == transfer.id


def test_no_question_without_a_complaint_or_without_transfers():
    cfg, b = _bundle("eth_usdc_transfer")
    assert for_bundle(b, "what happened here?", cfg) is None and for_bundle(b, None, cfg) is None


def test_an_address_that_received_nothing_is_said_from_the_transfers():
    cfg, b = _bundle("eth_usdc_transfer")
    other = "0x000000000000000000000000000000000000dEaD"
    fact = apply_answer(b, "expected_receipt", other)
    assert fact.kind == "triage" and "none of the movements of value the explorer lists" in fact.text
    assert other in fact.text
    assert fact.sources  # the transfers' own sources


def test_the_address_that_received_it_is_named():
    cfg, b = _bundle("eth_usdc_transfer")
    transfer = next(e for e in b.items if e.kind == "token_transfer")
    fact = apply_answer(b, "expected_receipt", transfer.data["to"].lower())
    assert transfer.id in fact.text and "went to" in fact.text


# ---- case 3: the cause rests on words borrowed from a known contract ----

def test_borrowed_words_ask_what_the_reader_was_doing():
    for fixture, config in [("eth_failed_unverified_bot", "ethereum-mainnet"), ("op_fail_deadline", "optimism-mainnet")]:
        cfg, b = _bundle(fixture, config)
        c = for_bundle(b, None, cfg)
        assert c.kind == "intent" and {o.id for o in c.options} == {"swap", "other"}, fixture


def test_not_swapping_lowers_the_borrowed_reading_to_unknown():
    cfg, b = _bundle("eth_failed_unverified_bot")
    fact = apply_answer(b, "intent", "other")
    diagnosis = next(e for e in b.items if e.kind == "diagnosis" and not e.data.get("from_replay"))
    assert diagnosis.data["label"] == "UNKNOWN" and diagnosis.text.startswith("UNKNOWN")
    assert fact.id in diagnosis.text and fact.sources[0].kind == "reader"


def test_swapping_keeps_the_reading_likely_never_higher():
    cfg, b = _bundle("eth_failed_unverified_bot")
    apply_answer(b, "intent", "swap")
    diagnosis = next(e for e in b.items if e.kind == "diagnosis" and not e.data.get("from_replay"))
    assert diagnosis.data["label"] == "LIKELY"


def test_a_confirmed_cause_needs_no_question():
    cfg, b = _bundle("eth_fail_expired_v2")
    assert for_bundle(b, None, cfg) is None


def test_no_question_when_the_config_allows_none():
    cfg, b = _bundle("eth_failed_unverified_bot")
    cfg = cfg.model_copy(update={"assistant": cfg.assistant.model_copy(update={"max_clarifying_questions": 0})})
    assert for_bundle(b, None, cfg) is None


# ---- the service and the API ----

def _answer(b, cfg, event_log, **kw):
    return answer_transaction(b.tx_hash, cfg, "support", write=WRITE, fresh=False, source="api", log=event_log,
                              store=None, build=lambda h, c: b, finality=lambda: None, record=lambda log, e: None,
                              write_fn=lambda bundle, c, mode, question=None: write_checked(
                                  bundle, c, mode, backend=Backend(), question=question), **kw)


def test_the_service_asks_before_writing_and_answers_after(event_log):
    cfg, b = _bundle("eth_failed_unverified_bot")
    asked = _answer(b, cfg, event_log)
    assert asked.clarify is not None and asked.text is None and asked.structured()["clarify"]["kind"] == "intent"
    cfg, b = _bundle("eth_failed_unverified_bot")
    answered = _answer(b, cfg, event_log, clarified={"kind": "intent", "answer": "other"})
    assert answered.clarify is None and answered.text and any(e.kind == "triage" for e in answered.bundle.items)


def test_one_question_at_most(event_log):
    # after the reader picked a transaction from an address's list, no second question (D2)
    cfg, b = _bundle("eth_failed_unverified_bot")
    result = _answer(b, cfg, event_log, clarified={"kind": "which_transaction", "answer": b.tx_hash})
    assert result.clarify is None and result.text


def test_the_api_returns_the_question_for_an_address(event_log, tmp_path):
    cfg = load_config(ETH)
    app = create_app(cfg, log=event_log, store=None, build=lambda h, c: replay_bundle(c, h, "eth_usdc_transfer"),
                     finality=lambda: None, probe_client=httpx.Client(transport=make_transport("health_ethereum")),
                     explorer_for=lambda: _explorer(cfg))
    body = TestClient(app, base_url=LOCAL).post("/explain", json={"hash": SENDER}).json()
    assert body["clarify"]["kind"] == "which_transaction" and body["clarify"]["options"][0]["id"] == USDC_TX


def test_the_api_asks_which_transaction_for_text(event_log, tmp_path):
    cfg = load_config(ETH)
    app = create_app(cfg, log=event_log, store=None, build=lambda h, c: replay_bundle(c, h, "eth_usdc_transfer"),
                     finality=lambda: None, probe_client=httpx.Client(transport=make_transport("health_ethereum")),
                     explorer_for=lambda: None)
    body = TestClient(app, base_url=LOCAL).post("/explain", json={"hash": "0x123"}).json()
    assert body["clarify"]["kind"] == "which_transaction" and body["clarify"]["options"] == []


def test_the_command_line_shows_the_question_when_it_cannot_ask(monkeypatch):
    from typer.testing import CliRunner
    from anychain import cli
    cfg = load_config(ETH)
    monkeypatch.setattr(cli, "ExplorerClient", lambda *a, **kw: _explorer(cfg))
    out = CliRunner().invoke(cli.app, ["explain", SENDER, "--config", ETH, "--json"])
    assert out.exit_code == 1 and '"which_transaction"' in out.stdout and USDC_TX in out.stdout


# ---- review of Phase 4 ----

def test_an_answer_to_a_question_never_asked_adds_nothing(event_log):
    # the deadline cause on the verified Uniswap router is CONFIRMED: no intent question applies there
    cfg, b = _bundle("eth_fail_expired_v2")
    result = _answer(b, cfg, event_log, clarified={"kind": "intent", "answer": "other"})
    assert not [e for e in result.bundle.items if e.kind == "triage"]
    cfg, b = _bundle("eth_fail_expired_v2")  # a failure: no "did not receive it" question either
    result = _answer(b, cfg, event_log, clarified={"kind": "expected_receipt", "answer": SENDER})
    assert not [e for e in result.bundle.items if e.kind == "triage"]


def test_native_value_sent_by_the_call_counts_as_a_payment():
    # real: the Uniswap v2 swap sent 0.2 ETH with the call to the router
    cfg, b = _bundle("eth_uniswap_v2_swap")
    overview = next(e for e in b.items if e.kind == "overview")
    fact = apply_answer(b, "expected_receipt", overview.data["to"])
    assert "went to that address" in fact.text and overview.id in fact.text


def test_the_scope_of_none_is_said():
    cfg, b = _bundle("eth_usdc_transfer")
    fact = apply_answer(b, "expected_receipt", "0x000000000000000000000000000000000000dEaD")
    assert "the explorer lists" in fact.text and "hides" in fact.text


def test_an_incomplete_transfer_list_is_said():
    cfg, b = _bundle("eth_usdc_transfer")
    b.add_gap("Token transfers", "more than 5 pages of transfers", "Open the explorer page", False,
              "not_interpretable")  # edit: the real gap the bundle declares past MAX_PAGES
    fact = apply_answer(b, "expected_receipt", "0x000000000000000000000000000000000000dEaD")
    assert "incomplete" in fact.text
