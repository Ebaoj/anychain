"""PHASE2_5 T2 (R2, D39): security notes are pattern matches on the called function's code, with the line."""
import json

import pytest

from anychain import security
from anychain.config import load_config
from anychain.solidity import SolidityIndex
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case


def _scan(body, header="external", extra="", name="go", contract="C"):
    src = {"c.sol": f"contract {contract} {{\n    address owner;\n    uint[] items;\n    mapping(address => uint) balances;"
                    f"\n{extra}\n    function {name}(address to) {header} {{\n{body}\n    }}\n}}"}
    index = SolidityIndex(src)
    f = next(f for f in index.contracts[contract].functions if f.name == name)
    return [n.pattern for n in security.scan(index, index.find(contract, f.selector), contract)]


@pytest.mark.parametrize("body, header, pattern", [
    ('        require(tx.origin == owner, "no");', "external", "tx_origin"),
    ("        (bool ok, ) = to.delegatecall(msg.data); require(ok);", "external", "delegatecall"),
    ("        to.call(msg.data);", "external", "unchecked_call"),
    ("        payable(to).send(1);", "external", "unchecked_call"),
    ('        (bool ok, ) = to.call{value: 1}("");\n        require(ok);\n        balances[to] = 0;', "external", "call_before_write"),
    ("        selfdestruct(payable(to));", "external", "selfdestruct"),
    ("        for (uint i; i < items.length; i++) { }", "external view", "unbounded_loop"),
])
def test_each_pattern_on_a_small_source(body, header, pattern):
    assert pattern in _scan(body, header)


def test_an_unprotected_sensitive_function_is_noted():
    assert _scan("        owner = to;", name="setOwner") == ["no_access_check"]


@pytest.mark.parametrize("body, header, name", [
    ('        require(tx.origin == msg.sender, "eoa");', "external", "go"),  # a check for contract callers
    ("        if (tx.origin == ESTIMATION) revert();", "external", "go"),  # gas estimation, not authorization
    ("        (bool ok, ) = to.call(msg.data);\n        require(ok);", "external", "go"),  # result used
    ('        (bool ok, ) = to.call{value: 1}("");\n        require(ok);\n        balances[to] = 0;',
     "external nonReentrant", "go"),  # guarded
    ("        locked = true;\n        (bool ok, ) = to.call{value: 1}(\"\");\n        require(ok);\n        balances[to] = 0;"
     "\n        locked = false;",
     "external", "go"),  # a hand-written lock
    ("        owner = to;\n        (bool ok, ) = to.call(\"\");\n        require(ok);\n        owner = address(0);",
     "external", "go"),  # set before the call, reset after: a lock
    ("        super.transfer(to, 1);\n        balances[to] = 0;", "external", "go"),  # super is not external
    ("        owner = to;", "external onlyOwner", "setOwner"),  # a modifier
    ('        require(msg.sender == owner, "no");\n        owner = to;', "external", "setOwner"),
    ("        _checkOwner();\n        owner = to;", "external", "setOwner"),
    ("        owner = to;", "external", "settle"),  # "settle" is not "set"
    ("        _setOwner(to);", "external", "setOwner"),  # a helper of the contract may hold the check
    ('        string memory s = "selfdestruct(x) tx.origin == owner";', "external", "go"),  # text in a string
    ("        // to.call(msg.data);", "external", "go"),  # a comment
])
def test_no_note_without_the_pattern(body, header, name):
    extra = "    bool locked;\n    function _setOwner(address a) internal { owner = a; }\n    function _checkOwner() internal view {}"
    assert _scan(body, header, extra=extra, name=name) == []


def test_a_selfdestruct_in_an_inherited_contract_is_noted():
    src = {"c.sol": "contract Base {\n    function kill() internal { selfdestruct(payable(msg.sender)); }\n}\n"
                    "contract C is Base {\n    function go() external { }\n}"}
    index = SolidityIndex(src)
    f = index.contracts["C"].functions[0]
    [note] = security.scan(index, index.find("C", f.selector), "C")
    assert note.pattern == "selfdestruct" and "Base" in note.text and note.line == 2


def _bundle(fixture):
    config, tx = _case(fixture)
    return replay_bundle(load_config(str(ROOT / "configs" / f"{config}.yaml")), tx, fixture)


def test_real_notes_are_a_fact_with_lines_and_the_heuristic_label():
    # a real Uniswap SwapRouter02 multicall on Ethereum (block 26148933): Multicall runs each item by delegatecall
    b = _bundle("eth_swaprouter02_multicall")
    [note] = [e for e in b.items if e.kind == "security_note"]
    assert "heuristic" in note.text and "not an audit" in note.text and "line 14 uses delegatecall to this contract's own code" in note.text
    assert "payable" in note.text and "msg.value" in note.text
    [code] = [e for e in b.items if e.kind == "code"]
    assert note.data["code"] == code.id and "14 |" in code.text and "delegatecall" in code.text


def test_no_note_fact_when_nothing_matches():
    assert not [e for e in _bundle("eth_brlc_set_pauser").items if e.kind == "security_note"]  # onlyOwner


def test_patterns_found_in_real_verified_contracts():
    # every match below was read by hand in the recorded verified source (D39)
    def index(fixture, name):
        for key, value in json.loads((ROOT / "tests" / "fixtures" / f"{fixture}.json").read_text()).items():
            if "/smart-contracts/" in key:
                meta = json.loads(value["body"])
                if meta.get("name") == name:
                    files = {meta["file_path"]: meta["source_code"]}
                    files.update({e["file_path"]: e["source_code"] for e in meta.get("additional_sources") or []})
                    return SolidityIndex(files)

    def notes(fixture, name, function):
        i = index(fixture, name)
        f = next(f for c in i.contracts.values() for f in c.functions if f.name == function and f.has_body)
        return {(n.pattern, n.line) for n in security.scan(i, i.find(name, f.selector), name)}
    assert notes("rootstock_fail_slippage", "SwapRouter02", "multicall") == {("delegatecall", 14)}
    assert notes("rootstock_fail_slippage", "SwapRouter02", "sweepToken") == {("no_access_check", 30)}
    # false alarms removed while calibrating on real code
    assert notes("celo_fee_token_no_decimals", "UniswapV3Pool", "swap") == set()  # its own lock, slot0.unlocked
    assert notes("op_user_deposit", "L2CrossDomainMessenger", "relayMessage") == set()  # xDomainMsgSender set/reset
    assert notes("rootstock_transfer", "RIFToken", "transferToContributor") == set()  # super.transfer is internal
    assert notes("eth_lido_submit", "Lido", "mintShares") == set()  # _auth(...)


# ---- review of T2: sentences that were false about the code ----

@pytest.mark.parametrize("body, header, name", [
    # a contract function named send (LayerZero endpoint, ERC777) is not Ethereum's low-level send
    ('        endpoint.send{value: msg.value}(1, to, "", payable(to), to, "");', "external payable", "go"),
    ('        tok.send(to, 1, "");', "external", "go"),
    # a call statement written over two lines, its result used
    ("        bool sent = payable(to)\n            .send(1);\n        require(sent);", "external", "go"),
    ("        bool ok = to == address(0)\n            ? false\n            : payable(to).send(1);\n        require(ok);",
     "external", "go"),
    # a local or a parameter with the name of a state variable is not the state variable
    ("        (bool ok, ) = to.call(\"\");\n        require(ok);\n        address owner = to;\n        owner = to;",
     "external", "go"),
    # exclusive branches: the write never runs after the call
    ("        if (to == address(0)) {\n            token.transfer(to, 1);\n        } else {\n            balances[to] = 0;\n        }",
     "external", "go"),
    # guard modifiers with other names
    ("        token.transfer(to, 1);\n        balances[to] = 0;", "external noReentrant", "go"),
    ("        token.transfer(to, 1);\n        balances[to] = 0;", "external reentrancyGuard", "go"),
    # an ETH transfer (2300 gas) is not a reentrancy setting
    ("        payable(to).transfer(1);\n        balances[to] = 0;", "external", "go"),
    # a loop over a local that shadows the state array, and a capped loop
    ("        uint[] memory items = new uint[](2);\n        for (uint i; i < items.length; i++) { }", "external", "go"),
    ("        for (uint i; i < items.length && i < 10; i++) { }", "external view", "go"),
])
def test_no_false_note(body, header, name):
    extra = "    contract Token { }\n    Token token;\n    address endpoint;\n    address tok;"
    assert _scan(body, header, extra=extra.replace("    contract Token { }\n", ""), name=name) == []


def test_an_explicit_base_call_is_internal():
    src = {"c.sol": "contract Base {\n    function transfer(address a, uint v) public virtual { }\n}\n"
                    "contract C is Base {\n    uint total;\n    function go(address to) external {\n"
                    "        Base.transfer(to, 1);\n        total = 0;\n    }\n}"}
    index = SolidityIndex(src)
    f = next(f for f in index.contracts["C"].functions if f.name == "go")
    assert security.scan(index, index.find("C", f.selector), "C") == []


def test_a_payable_parameter_does_not_make_the_function_payable():
    src = {"c.sol": "contract C {\n    function multicall(address payable refundTo, bytes[] calldata data) external {\n"
                    "        for (uint i; i < data.length; i++) {\n"
                    "            (bool ok, ) = address(this).delegatecall(data[i]); require(ok);\n        }\n    }\n}"}
    index = SolidityIndex(src)
    f = index.contracts["C"].functions[0]
    [note] = security.scan(index, index.find("C", f.selector), "C")
    assert "multicall pattern" in note.text and "payable" not in note.text


def test_a_single_self_delegatecall_is_not_called_the_multicall_pattern():
    src = {"c.sol": "contract C {\n    function go(bytes calldata data) external payable {\n"
                    "        (bool ok, ) = address(this).delegatecall(data); require(ok);\n    }\n}"}
    index = SolidityIndex(src)
    f = index.contracts["C"].functions[0]
    [note] = security.scan(index, index.find("C", f.selector), "C")
    assert "multicall" not in note.text and "items" not in note.text


@pytest.mark.parametrize("body", [
    "        return to.call(\"\");",  # returned: the caller sees it
    "        if (!payable(to).send(1)) revert();",
    "        (bool ok, ) =\n            to.call(\"\");\n        require(ok);",
])
def test_results_taken_in_other_forms(body):
    assert "unchecked_call" not in _scan(body)


def test_a_permission_check_from_a_library_counts():
    assert _scan("        LibAuth.enforceIsOwner();\n        owner = to;", name="setOwner") == []


def test_a_write_after_a_token_call_is_noted():
    assert _scan("        token.transfer(to, 1);\n        balances[to] = 0;", extra="    address token;") == ["call_before_write"]


# ---- second review of T2 ----

def _notes(body, header="external", extra="", name="go"):
    src = {"c.sol": f"library SafeCall {{\n    function call(address a, uint g, uint v, bytes memory d) internal returns (bool) {{ }}\n}}\n"
                    f"contract C {{\n    uint total;\n    uint[] items;\n    address token;\n    address bridge;\n{extra}\n"
                    f"    function {name}(address to) {header} {{\n{body}\n    }}\n}}"}
    index = SolidityIndex(src)
    f = next(f for f in index.contracts["C"].functions if f.name == name)
    return security.scan(index, index.find("C", f.selector), "C"), src["c.sol"].split("\n")


def _line_of(lines, needle):
    return next(n for n, text in enumerate(lines, 1) if needle in text)


def test_the_write_and_the_loop_are_cited_at_their_own_lines():
    notes, lines = _notes("        token.transfer(to, 1);\n        total = 2;")
    assert [n.line for n in notes] == [_line_of(lines, "total = 2")]
    notes, lines = _notes("        for (uint i; i < items.length; i++) { }", header="external view")
    assert [n.line for n in notes] == [_line_of(lines, "for (uint i")]


@pytest.mark.parametrize("body", [
    "        if (to == address(0)) token.transfer(to, 1); else { total = 1; }",  # a braceless if, braced else
    "        _check(payable(to).send(1));",  # the result passed to a function
    "        SafeCall.call(to, gasleft(), 1, \"\");",  # a library function named call
    "        bridge.send(1);",  # a contract function named send (the receiver is not an address payable)
])
def test_no_false_note_second_review(body):
    notes, _lines = _notes(body, extra="    function _check(bool ok) internal { }")
    assert notes == []


def test_an_ether_send_on_an_address_payable_is_still_seen():
    notes, _lines = _notes("        payable(to).send(1);")
    assert [n.pattern for n in notes] == ["unchecked_call"]
    notes, _lines = _notes("        owner.send(1);", extra="    address payable owner;")
    assert [n.pattern for n in notes] == ["unchecked_call"]


def test_the_access_note_says_only_what_the_header_shows():
    notes, _lines = _notes("        revert(\"disabled\");", name="renounceAll")
    [note] = notes
    assert "not marked view or pure" in note.text and "can change state" not in note.text


def test_a_selfdestruct_in_another_function_of_the_same_contract_is_noted():
    notes, lines = _notes("        total = 1;", extra="    function kill() internal { selfdestruct(payable(msg.sender)); }")
    [note] = notes
    assert note.pattern == "selfdestruct" and note.line == _line_of(lines, "selfdestruct(")
