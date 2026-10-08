"""PHASE2_5 T1 (R1): the code of the called function, and of the function raising a failure's reason, as facts."""
from anychain.config import load_config
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case


def _bundle(fixture):
    config, tx = _case(fixture)
    return replay_bundle(load_config(str(ROOT / "configs" / f"{config}.yaml")), tx, fixture)


def _code(b):
    return [e for e in b.items if e.kind == "code"]


def test_called_function_code_comes_from_the_verified_source():
    [fact] = _code(_bundle("eth_brlc_set_pauser"))
    assert "function setPauser(address newPauser) external onlyOwner" in fact.text
    assert "97 |" in fact.text  # numbered lines, so a line can be quoted
    # the deployed code's own layout (the repo mirror is a later version: contracts/base/common/..., lines 103-111)
    assert fact.data["path"] == "contracts/base/PausableExtUpgradeable.sol" and fact.data["lines"] == [97, 105]
    assert [s.kind for s in fact.sources] == ["explorer_api"]  # the deployed code's verified source


def test_code_is_found_without_any_repo_configured():
    # Uniswap's SwapRouter02 on Rootstock: no repo configured for that network, the verified source suffices
    facts = _code(_bundle("rootstock_fail_slippage"))
    assert any("function exactOutputSingle" in f.text for f in facts)


def test_the_line_raising_the_reason_is_shown_in_its_function():
    facts = _code(_bundle("rootstock_fail_slippage"))
    [raising] = [f for f in facts if f.data.get("raises")]
    assert raising.data["raises"] == "Too much requested" and "220 |" in raising.text
    assert "'Too much requested'" in raising.text


def test_long_functions_are_cut_and_say_so():
    from anychain.bundle import MAX_CODE_LINES
    [fact] = _code(_bundle("op_user_deposit"))  # a real 99-line function
    assert fact.data["lines"] == [222, 320]
    shown = [line for line in fact.text.splitlines() if " | " in line]
    assert len(shown) == MAX_CODE_LINES and "(19 more lines not shown)" in fact.text


def test_the_code_of_a_delegation_set_by_the_transaction_is_its_delegates():
    for fixture in ("eth_7702_execute", "eth_7702_set_then_revoked"):  # the second was revoked later
        [fact] = _code(_bundle(fixture))
        assert "from the verified source of the delegate 0x63c0c19a282a1B52b07dD5a65b58948A07DAE32B this " \
               "transaction set" in fact.text


def test_no_code_when_the_transaction_clears_the_delegation():
    assert _code(_bundle("eth_7702_revoke")) == []


def test_a_proxys_code_says_it_is_todays_implementation():
    [fact] = [f for f in _code(_bundle("celo_fail_balance_confirmed")) if f.data.get("raises")]
    assert "the implementation the explorer lists today" in fact.text and "may have been upgraded" in fact.text


def test_no_code_for_an_unverified_contract():
    assert _code(_bundle("eth_failed_unverified_bot")) == []


# ---- review of T1: the raising line is claimed only when the source proves where the text is written ----

import json  # noqa: E402

from anychain.solidity import SolidityIndex  # noqa: E402


def _verified_index(fixture, name):
    """The verified source of the contract called `name` in a real recording."""
    for key, value in json.loads((ROOT / "tests" / "fixtures" / f"{fixture}.json").read_text()).items():
        if "/smart-contracts/" in key:
            meta = json.loads(value["body"])
            if meta.get("name") == name:
                files = {meta["file_path"]: meta["source_code"]}
                files.update({e["file_path"]: e["source_code"] for e in meta.get("additional_sources") or []})
                return SolidityIndex(files)
    raise AssertionError(f"{name} not in {fixture}")


def test_a_reason_written_in_several_functions_names_none_of_them():
    vat = _verified_index("eth_maker_vat", "Vat")
    assert len(vat.literal("Vat/not-allowed")) == 3  # flux, move, fork: which one ran is not known
    assert vat.raising_function("Vat", "Vat/not-allowed") is None
    move = next(f for f in vat.contracts["Vat"].functions if f.name == "move")
    found, line = vat.raising_function("Vat", "Vat/not-allowed", move.selector)  # the call says which one ran
    assert found.function is move and move.start <= line <= move.end


def test_a_reason_written_once_names_its_function():
    vat = _verified_index("eth_maker_vat", "Vat")
    once = [text for text in ("Vat/dust", "Vat/ceiling-exceeded") if len(vat.literal(text)) == 1]
    assert once
    found, _line = vat.raising_function("Vat", once[0])
    assert found.contract.name == "Vat"


def test_a_reason_in_a_contract_that_was_not_called_names_nothing():
    dai_join = _verified_index("eth_maker_vat", "DaiJoin")
    assert dai_join.literal("GemJoin/failed-transfer")  # GemJoin is declared in the same file
    assert dai_join.raising_function("DaiJoin", "GemJoin/failed-transfer") is None


def test_an_overridden_base_function_is_not_named():
    src = {"a.sol": '''contract Base {
    function go() external virtual { require(false, "only-here"); }
}
contract Top is Base {
    function go() external override { }
}'''}
    index = SolidityIndex(src)
    assert index.raising_function("Top", "only-here") is None
    assert index.raising_function("Base", "only-here")[0].function.name == "go"


def test_the_raising_line_is_shown_even_past_the_cut():
    from anychain.bundle import MAX_CODE_LINES, render_code
    lines = [f"x{n};" for n in range(200)]
    text = render_code(lines, 1000, raise_line=1180)
    shown = [int(line.split(" | ")[0]) for line in text.splitlines() if " | " in line]
    assert 1180 in shown and len(shown) <= MAX_CODE_LINES and "not shown" in text


def test_numbers_and_hex_inside_code_are_not_evidence_values():
    # review of T1: constants in contract code ("5000") must not let an amount through; line numbers shown do
    from anychain.validator import check_answer
    evidence = json.dumps({"evidence": [
        {"id": "E1", "kind": "code", "fact": "Code of f() in C (c.sol, lines 1180-1182), from ...:\n"
                                             "1180 | uint x = 5000;\n1181 | address a = 0x1111111111111111111111111111111111111111;\n1182 | }"},
        {"id": "E2", "kind": "transfer", "fact": "Transfer of 12 USDC"}]})
    assert check_answer("Line 1181 sets it [E1].", evidence, set(), {"E1", "E2"}) == []
    assert check_answer("5000 USDC moved [E1].", evidence, set(), {"E1", "E2"})
    assert check_answer("Sent to 0x1111111111111111111111111111111111111111 [E1].", evidence, set(), {"E1", "E2"})
    assert check_answer("Line 1500 [E1].", evidence, set(), {"E1", "E2"})


# ---- second review of T1 ----

def test_a_modifier_writing_the_same_reason_names_no_line():
    src = {"a.sol": '''contract C {
    modifier m() { require(msg.sender != address(0), "nope"); _; }
    function go() external m { require(false, "nope"); }
}'''}
    index = SolidityIndex(src)
    go = index.contracts["C"].functions[0]
    assert index.raising_function("C", "nope", go.selector) is None  # the modifier runs first


def test_line_numbers_count_only_newlines():
    src = {"a.sol": "contract C {\n// note more\nfunction f() external {\n require(false, \"x\");\n}\n}"}
    index = SolidityIndex(src)
    f = index.contracts["C"].functions[0]
    assert index.lines("a.sol", f.start, f.start)[0].startswith("function f()")
    assert index.literal("x") == [("a.sol", 4)]


def test_a_code_line_quoted_exactly_may_hold_its_constants():
    from anychain.validator import check_answer
    evidence = json.dumps({"evidence": [
        {"id": "E1", "kind": "code", "fact": "Code of f() in C (c.sol, lines 140-142):\n140 | x;\n"
                                             "141 | y;\n142 | require(fee <= 10000, \"fee\");"}]})
    assert check_answer("Line 142: `require(fee <= 10000, \"fee\");` [E1].", evidence, set(), {"E1"}) == []
    assert check_answer("Line 142: `require(fee <= 20000)` [E1].", evidence, set(), {"E1"})
    assert check_answer("The fee was 10000 [E1].", evidence, set(), {"E1"})  # outside a quote: not a value
