"""The Solidity index on small sources: every case from the clean-context review of T6 (written first).
A function must never get a wrong selector or a wrong defining contract; when in doubt, none."""
from anychain.reads import selector
from anychain.solidity import SolidityIndex


def _index(**files):
    return SolidityIndex({f"{name}.sol": text for name, text in files.items()})


def _sig(index, contract):
    return {f.signature for f in index.contracts[contract].functions}


def test_imported_types_it_cannot_see_get_no_selector():
    idx = _index(A='''import {Price, Order, Side} from "./lib.sol";
        contract A { function set(Price p) external {} function fill(Order calldata o) external {}
                     function pick(Side s) external {} }''')
    assert _sig(idx, "A") == {None}


def test_address_payable_arrays_keep_their_array():
    idx = _index(A="contract A { function pay(address payable[] memory to) external {} }")
    assert _sig(idx, "A") == {"pay(address[])"}


def test_value_types_resolve_to_their_underlying_type():
    idx = _index(A="type Price is uint128; contract A { function set(Price p) external {} }")
    assert _sig(idx, "A") == {"set(uint128)"}


def test_same_struct_name_in_two_libraries_needs_its_qualifier():
    idx = _index(L='''library L1 { struct Order { uint256 a; } } library L2 { struct Order { address b; bool c; } }
        contract A { function fill(L1.Order calldata o) external {} }
        contract B { function fill(Order calldata o) external {} }''')
    assert _sig(idx, "A") == {"fill((uint256))"}  # the qualified one is L1's (it was L2's before)
    assert _sig(idx, "B") == {None}  # unqualified and declared twice: not resolvable


def test_strings_and_comments_create_nothing():
    idx = _index(A='''// The contract Price is documented elsewhere
        contract A { string constant DOC = "call function transfer(address,uint256) first";
                     function set(Price p) external {} }''')
    assert _sig(idx, "A") == {None}  # no phantom transfer(), and Price is not a contract


def test_recursive_struct_does_not_crash():
    idx = _index(A="contract A { struct N { N[] kids; } function f(N calldata n) external {} }")
    assert _sig(idx, "A") == {None}


def test_internal_and_private_functions_have_no_selector():
    idx = _index(A='''contract A { function burn(uint256 x) internal {} function _p(uint256 x) private {}
                     function pub(uint256 x) public {} }''')
    assert idx.find("A", selector("burn(uint256)")) is None
    assert idx.find("A", selector("pub(uint256)")).function.name == "pub"


def test_lookup_follows_c3_linearization():
    # D is B, C; B overrides f from A. Solidity's order for D: D, C, B, A -> B.f (C does not define f).
    idx = _index(A='''contract A { function f() public virtual {} }
        contract B is A { function f() public virtual override {} }
        contract C is A { }
        contract D is B, C { }''')
    assert idx.find("D", selector("f()")).contract.name == "B"


def test_declarations_without_a_body_are_never_the_definition():
    idx = _index(A='''interface IT { function setX(uint256 x) external; }
        abstract contract Base { function g() public virtual; }
        contract Impl { function setX(uint256 x) external virtual {} }
        contract T is Impl, IT { }''')
    assert idx.find("T", selector("setX(uint256)")).contract.name == "Impl"
    assert idx.find("Base", selector("g()")) is None


def test_duplicate_contract_names_are_ambiguous():
    idx = SolidityIndex({"a/mocks/Token.sol": "contract Token { function f() external {} }",
                         "b/Token.sol": "contract Token { function f() external { } }"})
    assert idx.ambiguous == {"Token"} and idx.find("Token", selector("f()")) is None


def test_formatting_does_not_make_two_copies_differ():
    a = _index(A="contract A { function f(uint256 x) external returns (uint256) { return x+1; } }")
    b = _index(A="contract A {\n  function f(uint256 x) external returns (uint256) {\n    return x + 1;\n  }\n}")
    sel = selector("f(uint256)")
    assert a.find("A", sel).function.text == b.find("A", sel).function.text


def test_literal_search_ignores_comments():
    idx = _index(A='''contract A { // revert("NOT_YET") was the old message
        function f() external { require(false, "NOT_YET"); } }''')
    assert idx.literal("NOT_YET") == [("A.sol", 2)]
