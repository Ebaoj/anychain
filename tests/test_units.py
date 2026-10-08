"""D49: token amounts in the token's own units, from its decimals() and symbol() read on the node (real answers
recorded for USDT on Celo at block 78962883: decimals 6, symbol "USD₮")."""
import json

import pytest

from anychain.config import load_config
from anychain.units import in_units
from anychain.validator import allowed_urls, check_answer, evidence_ids
from anychain.writer import evidence_payload
from tests.conftest import ROOT, replay_bundle
from tests.test_chat import RECIPIENT, TOKEN, Scripted, _fact_with, _session
from tests.test_golden import _case


@pytest.mark.parametrize("raw, decimals, text", [
    (33540, 6, "0.03354"), (1445, 6, "0.001445"), (106500, 6, "0.1065"), (10**18, 18, "1"), (0, 6, "0"),
    (123, 0, "123"), (1, 18, "0.000000000000000001"), (2**256 - 1, 18,
                                                     "115792089237316195423570985008687907853269984665640564039457.584007913129639935"),
])
def test_exact_conversion(raw, decimals, text):
    assert in_units(raw, decimals) == text


def test_the_diagnosis_gives_both_amounts_in_the_tokens_units():
    _c, tx = _case("celo_fail_balance_confirmed")
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    b = replay_bundle(cfg, tx, "celo_fail_balance_confirmed")
    [d] = [e for e in b.items if e.kind == "diagnosis"]
    assert "1445 raw units (0.001445 USD₮)" in d.text and "106500 raw units (0.1065 USD₮)" in d.text
    assert "decimals() = 6" in d.text
    reads = [e for e in b.items if e.kind == "state_read"]
    assert any("decimals" in e.text and "returned 6" in e.text for e in reads)  # the decimals are a fact too
    # a written answer may quote the converted amounts, in the Portuguese format too
    answer = f"O saldo era 0,001445 USD₮, menor que 0,1065 USD₮ [{d.id}]."
    assert check_answer(answer, evidence_payload(b, cfg), allowed_urls(b), evidence_ids(b)) == []


def test_a_chat_read_of_a_balance_is_converted():
    session = _session()
    ask = json.dumps({"tools": [{"tool": "read", "contract": TOKEN, "function": "balanceOf(address)",
                                 "args": [RECIPIENT], "returns": "uint256"}]})
    turn = session.ask("x", Scripted(ask, lambda u: f"It held 0,03354 USD₮ [{_fact_with(u, 'Asked in the chat')}]."))
    [fact] = [e for e in session.bundle.items if e.id in turn.new_facts]
    assert "That is 0.03354 USD₮, with the token's decimals() = 6" in fact.text and turn.outcome == "ok"
    assert len(fact.sources) == 3  # the balance, decimals() and symbol() reads


def test_a_number_that_is_not_an_amount_is_not_converted():
    session = _session()
    ask = json.dumps({"tools": [{"tool": "read", "contract": TOKEN, "function": "decimals()", "args": [],
                                 "returns": "uint8"}]})
    turn = session.ask("x", Scripted(ask, "It failed [E1]."))
    facts = [e for e in session.bundle.items if e.id in turn.new_facts]
    assert all("That is" not in f.text for f in facts)


# ---- review of D49 ----

def _celo():
    _c, tx = _case("celo_fail_balance_confirmed")
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    return cfg, replay_bundle(cfg, tx, "celo_fail_balance_confirmed")


@pytest.mark.parametrize("answer", [
    "The sender held 1445 USD₮ [E1].",  # the raw number with the symbol: a millionfold overstatement
    "It tried to send 106500 USD₮ [E1].",
    "Tinha 1.445 USD₮ [E1].",  # 1445 in the Portuguese format
    "Tentou 106.500 USD₮ [E1].",
    "Tinha 0,00 USD₮ [E1].",  # a balance rounded to zero
])
def test_a_number_before_a_tokens_symbol_must_be_its_converted_amount(answer):
    cfg, b = _celo()
    assert check_answer(answer, evidence_payload(b, cfg), allowed_urls(b), evidence_ids(b))


@pytest.mark.parametrize("answer", ["Tinha 0,001445 USD₮ e tentou 0,1065 USD₮ [E1].",
                                    "It held 0.001445 USD₮ [E1].", "The raw balance was 1445 [E1]."])
def test_the_converted_amounts_and_raw_numbers_alone_still_pass(answer):
    cfg, b = _celo()
    assert check_answer(answer, evidence_payload(b, cfg), allowed_urls(b), evidence_ids(b)) == []


def test_the_symbol_is_said_to_be_the_tokens_own_claim():
    _cfg, b = _celo()
    [d] = [e for e in b.items if e.kind == "diagnosis"]
    assert "the name the contract gives itself" in d.text


@pytest.mark.parametrize("symbol, used", [("USD₮", True), ("USDC", True), ("USD 1000000", False), ("A" * 25, False),
                                          ("WIN$", True), ("x‮", False)])
def test_only_a_plain_symbol_is_used_in_amounts(symbol, used):
    from anychain.reads import Read, TokenUnits
    units = TokenUnits(6, symbol, Read("decimals()", "0x1", (), 1, 6), None)
    assert (symbol in units.amount(1445)) is used


def test_an_unread_decimals_leaves_a_gap_so_the_answer_is_not_kept():
    _c, tx = _case("celo_fail_balance_confirmed")
    cfg = load_config(str(ROOT / "configs" / "celo-mainnet.yaml"))
    busy = {'eth_call [{"data": "0x313ce567", "to": "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"}, "0x4b4e0c3"]':
            {"status": 503, "body": "busy"}}
    b = replay_bundle(cfg, tx, "celo_fail_balance_confirmed", overrides=busy)
    [d] = [e for e in b.items if e.kind == "diagnosis"]
    assert "1445 raw units," in d.text and "USD₮" not in d.text  # raw amounts stand alone
    assert any(g.retryable and "decimals" in g.why for g in b.gaps)


def test_only_erc20_amount_signatures_are_converted():
    from anychain.units import is_amount
    assert is_amount("balanceOf(address)") and is_amount("allowance(address,address)") and is_amount("totalSupply()")
    assert not is_amount("balanceOf(address,uint256)")  # ERC-1155: a count of one id
    assert not is_amount("allowance(address,address,address)")  # Permit2
