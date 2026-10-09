"""The answer check (PHASE2 R7), on real model answers and on tampered copies of them."""
import json

import pytest

from anychain.config import load_config
from anychain.validator import allowed_urls, check_answer, evidence_ids
from anychain.writer import evidence_payload
from tests.conftest import ROOT, replay_bundle

RECORDED = json.loads((ROOT / "tests" / "llm_answers" / "eth_relay_swap.json").read_text())


@pytest.fixture(scope="module")
def bundle():
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    return replay_bundle(cfg, RECORDED["tx"], RECORDED["fixture"])


def _check(text, bundle):
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    return check_answer(text, evidence_payload(bundle, cfg), allowed_urls(bundle), evidence_ids(bundle))


def test_real_support_answer_passes(bundle):
    assert _check(RECORDED["answers"]["support"], bundle) == []


def test_real_developer_answer_is_caught_adding_a_standard_from_memory(bundle):
    # The model wrote "PoolManager, ERC-6909": a true fact about Uniswap v4, but not in the evidence.
    assert "ERC-6909" in RECORDED["answers"]["developer"]
    assert _check(RECORDED["answers"]["developer"], bundle) == ["the number 6909 is not in the evidence"]


@pytest.mark.parametrize("original, tampered, problem", [
    ("[E4]", "[E999]", "E999 is cited"),
    ("985,670661 USDC", "985,970661 USDC", "985,970661"),                      # pt decimal, one digit off
    ("5.653.200,67", "5.953.200,67", "5.953.200,67"),                          # pt thousands
    ("0,003043284794073318 ETH", "0,004043284794073318 ETH", "0,004043284794073318"),
    ("0xf70d…dbEF", "0xf70d…dbEE", "0xf70d…dbEE"),                              # abbreviation fits no address
    ("https://eth.blockscout.com/tx/0xd58d", "https://evil.example/tx/0xd58d", "evil.example"),
])
def test_tampered_support_answer_is_caught(original, tampered, problem, bundle):
    answer = RECORDED["answers"]["support"]
    assert original in answer
    problems = _check(answer.replace(original, tampered, 1), bundle)
    assert any(problem in p for p in problems), problems


@pytest.mark.parametrize("added", [
    "O remetente recebeu 1.500 USDC de volta.",                 # an amount nowhere in the evidence
    "O contrato 0x1111111111111111111111111111111111111111 também participou.",
    "Equivale a cerca de 12,4 milhões de VERIDIA.",             # a scaled number that fits nothing
])
def test_invented_sentences_are_caught(added, bundle):
    assert _check(RECORDED["answers"]["support"] + "\n" + added, bundle)


@pytest.mark.parametrize("legit", [
    "Cerca de 5,65 milhões de VERIDIA.",                        # rounded, with a scale word
    "Gas: 916.302 de 1.229.884.",                                # pt thousands
    "Fee 0.00304 ETH.",                                          # en, cut to fewer decimals
    "Bloco 26084739.",
])
def test_legitimate_ways_of_writing_numbers_pass(legit, bundle):
    assert _check(legit, bundle) == []


# ---- write, check, retry once, withhold (wired into explain) -------------------------------

class Scripted:
    """A backend that replies with the given answers in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def complete(self, system, user):
        self.sent.append(user)
        return self.replies.pop(0)


def test_good_answer_goes_out_on_the_first_try(bundle):
    from anychain.writer import write_checked
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    backend = Scripted(RECORDED["answers"]["support"])
    checked = write_checked(bundle, cfg, "support", backend)
    assert (checked.outcome, len(backend.sent)) == ("ok", 1)


def test_bad_answer_is_retried_with_its_problems(bundle):
    from anychain.writer import write_checked
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    backend = Scripted(RECORDED["answers"]["developer"], RECORDED["answers"]["support"])
    checked = write_checked(bundle, cfg, "support", backend)
    assert checked.outcome == "retried" and checked.text == RECORDED["answers"]["support"]
    assert "the number 6909 is not in the evidence" in backend.sent[1]  # the model is told what was wrong


def test_twice_bad_answer_is_withheld(bundle):
    from anychain.writer import write_checked
    cfg = load_config(str(ROOT / "configs" / "ethereum-mainnet.yaml"))
    bad = RECORDED["answers"]["support"] + "\nRecebeu 1.500 USDC."
    checked = write_checked(bundle, cfg, "support", Scripted(bad, bad))
    assert checked.text is None and checked.outcome == "withheld"
    assert checked.problems == ["the number 1.500 is not in the evidence"]


def test_explain_withholds_and_logs_it(monkeypatch, event_log):
    from typer.testing import CliRunner

    from anychain import cli, writer
    bad = RECORDED["answers"]["support"] + "\nRecebeu 1.500 USDC."
    monkeypatch.setattr(cli, "build_bundle", lambda h, cfg: replay_bundle(cfg, h, RECORDED["fixture"]))
    monkeypatch.setattr(writer, "backend_for", lambda llm: Scripted(bad, bad))
    monkeypatch.setattr(cli, "SqliteEventLog", lambda _p, **_kw: event_log)
    eth = str(ROOT / "configs" / "ethereum-mainnet.yaml")
    out = CliRunner().invoke(cli.app, ["explain", RECORDED["tx"], "--config", eth]).output
    assert "withheld" in out and "Recebeu 1.500 USDC" not in out and "**[E1]**" in out
    [row] = event_log.summary(0)
    assert (row["withheld"], row["degraded"]) == (1, 1)
    [problem] = event_log.problems(0)  # findable, with what the model invented
    assert problem["writer"] == "withheld"
    assert problem["check_failures"] == [{"name": "answer_check", "detail": "the number 1.500 is not in the evidence"}]
    out = CliRunner().invoke(cli.app, ["log", "--config", eth, "--problems"]).output
    assert "written answer withheld" in out and "the number 1.500" in out


def test_old_logs_get_the_writer_column(tmp_path):
    import sqlite3

    from anychain.events import SqliteEventLog
    path = tmp_path / "old-runs.db"  # runs.db here is the suite's own log
    with sqlite3.connect(path) as db:  # the schema before PHASE2 T1
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL NOT NULL, network TEXT NOT NULL, "
                   "tx_hash TEXT NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, "
                   "status TEXT, facts INTEGER NOT NULL, error TEXT)")
    SqliteEventLog(path)
    with sqlite3.connect(path) as db:
        assert "writer" in {row[1] for row in db.execute("PRAGMA table_info(runs)")}


def test_old_log_can_be_read_without_changing_it(tmp_path):
    import sqlite3

    from anychain.events import SqliteEventLog
    path = tmp_path / "old-runs.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts REAL NOT NULL, network TEXT NOT NULL, "
                   "tx_hash TEXT NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER NOT NULL, "
                   "status TEXT, facts INTEGER NOT NULL, error TEXT)")
        db.execute("CREATE TABLE gaps (run_id INTEGER, topic TEXT, cause TEXT, why TEXT, retryable INTEGER)")
        db.execute("CREATE TABLE checks (run_id INTEGER, name TEXT, status TEXT, detail TEXT)")
        db.execute("INSERT INTO runs VALUES (1, 1.0, 'celo-mainnet', '0x1', 'cli', 'ok', 5, 'success', 3, NULL)")
    before = path.read_bytes()
    [row] = SqliteEventLog(path, read_only=True).summary(0)
    assert row["answers"] == 1 and row["withheld"] is None and path.read_bytes() == before


# ---- cases from the clean-context review of T1 (written before the fixes) --------------------

@pytest.mark.parametrize("invented", [
    "Contrato 0x0000000000000000000000000000000000000001 [E4].",        # padded address inside calldata
    "Valor 0x000000000000000000000000000000000000000003ac02405.",          # a long calldata slice
    "Endereço 0xd58d09060a633d461c0d260bfdb4f61bd4ab84a9 [E1].",           # tx hash prefix posing as an address
    "Ver [E3, E999].", "Ver E999.", "Ver [e999].", "Ver [E 999].",          # citation variants
    "Recebeu 1 500 000 USDC [E4].", "Recebeu 1 500 000 USDC.",  # spaces as thousands separators
    "Cerca de 12 mi de VERIDIA.", "Cerca de 12 bi.",                        # short scale words
    "Cerca de 1 milhão de VERIDIA.",                                        # one-digit scale: matches anything
    "Recebeu USDC1500.",                                                    # number glued after letters
    "Recebeu 0,5 ETH.",                                                     # a small amount with decimals
    "amount0 = -794929588",                                                 # sign added (evidence: 794929588)
    "Veja www.evil.example/0xd58d.",                                        # link without scheme
    "De 0xf7… para outro.",                                                 # abbreviation too short to identify
])
def test_review_false_negatives_are_caught(invented, bundle):
    assert _check(invented, bundle), invented


URL_TX = "https://eth.blockscout.com/tx/0xd58d09060a633d461c0d260bfdb4f61bd4ab84a9ba5039b3f5f9ac5effe935a3"


@pytest.mark.parametrize("legit", [
    f"Veja **{URL_TX}**", f"Veja _{URL_TX}_", f"Veja {URL_TX}!", f"Veja {URL_TX}/", f"Veja {URL_TX}#logs",
    "Cerca de 5 653 200,67 VERIDIA.", "Data 29.09.2026.", "De 0xf70d...e foi para outro.",
])
def test_review_false_positives_pass(legit, bundle):
    assert _check(legit, bundle) == [], legit


def test_digits_inside_a_name_are_not_a_number():
    # found by the eval (PHASE3 T5): "ERC1967Proxy", a contract name in the evidence, was read as the number 1967
    evidence = json.dumps({"evidence": [{"id": "E1", "kind": "call", "fact": "Called upgradeTo on 0x1 (ERC1967Proxy)."}]})
    assert check_answer("The proxy ERC1967Proxy forwarded the call [E1].", evidence, set(), {"E1"}) == []
    assert check_answer("It moved 1967 tokens [E1].", evidence, set(), {"E1"})  # a real number still checked


def test_a_shortened_address_is_not_a_citation():
    # real: eval of 2026-10-09, case out_of_gas: Claude shortened 0x…408E624 as "0x52b2…E624" (the support prompt
    # allows it) and the check read "E624" as a citation of a fact that does not exist, withholding a correct answer
    from anychain.validator import _citations
    from anychain.writer import normalize_citations
    cited, _rest = _citations("Foi para 0x52b2…E624 e 0xdAC1...E624 [E5].")
    assert cited == {5}
    assert normalize_citations("para 0x52b2…E624 [E5]") == "para 0x52b2…E624 [E5]"
