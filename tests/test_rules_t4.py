"""PHASE2_5 T4 (R5, R6): a deadline compared with the block's time, and access-control failures."""
import pytest

from anychain.config import load_config
from anychain.diagnosis import Context, diagnose, label_for
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case


def _bundle(fixture, config="ethereum-mainnet"):
    _c, tx = _case(fixture)
    return replay_bundle(load_config(str(ROOT / "configs" / f"{config}.yaml")), tx, fixture)


def _finding(b):
    [finding] = [e for e in b.items if e.kind == "diagnosis"]
    return finding


def test_a_passed_deadline_is_proven_by_the_call_and_the_block():
    # Real Uniswap V2 swap (block 26149091, 2026-10-08T17:11:47Z) whose deadline parameter is 2400
    finding = _finding(_bundle("eth_fail_expired_v2"))
    assert "deadline parameter is 2400 (1970-01-01T00:40:00Z)" in finding.text
    assert "block's time is 1791479507 (2026-10-08T17:11:47Z)" in finding.text
    assert "had passed" in finding.text and finding.data["computed"]["deadline_check"]["deadline_passed"] is True


def _ctx(**kw):
    base = dict(reason=None, result=None, call=None, sender=None, to=None, to_text="the contract", block=1,
                gas_used=1, gas_limit=100, reader=None)
    return Context(**{**base, **kw})


def test_a_deadline_still_ahead_is_not_called_the_cause():
    ctx = _ctx(call={"function": "swap", "args": {"deadline": 2_000_000_000}}, block_time=1_791_479_507)
    assert diagnose(ctx).rule != "deadline"


def test_another_reason_wins_over_a_passed_deadline():
    from anychain.collectors.types import RevertReason
    reason = RevertReason(method_call="Error(string reason)", parameters=(("reason", "Paused"),), raw_data=None,
                          original={})
    ctx = _ctx(reason=reason, call={"function": "swap", "args": {"deadline": 2400}}, block_time=1_791_479_507)
    assert diagnose(ctx).rule != "deadline"


# ---- access control (R5) ----

from anychain.collectors.types import RevertReason  # noqa: E402
from anychain.reads import Read  # noqa: E402

SENDER, OTHER, TARGET = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "cc" * 20
ROLE = "0x" + "9f" * 32


def _text(t):
    return RevertReason(method_call="Error(string reason)", parameters=(("reason", t),), raw_data=None, original={})


def _custom(call, params):
    return RevertReason(method_call=call, parameters=tuple(params), raw_data=None, original={})


class FakeReader:
    def __init__(self, owner=None, has_role=None):
        self.owner_value, self.role_value, self.calls = owner, has_role, []

    def owner(self, contract, block):
        self.calls.append(("owner", contract, block))
        return Read("owner()", contract, (), block, self.owner_value)

    def has_role(self, contract, role, account, block):
        self.calls.append(("hasRole", contract, role, account, block))
        return Read("hasRole(bytes32,address)", contract, (role, account), block, self.role_value)


def _access(reason, reader):
    return diagnose(_ctx(reason=reason, sender=SENDER, to=TARGET, block=100, reader=reader))


def test_ownable_confirmed_when_the_sender_is_not_the_owner():
    reader = FakeReader(owner=OTHER)
    f = _access(_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]), reader)
    assert f.rule == "access_control" and f.level == "confirmed" and reader.calls == [("owner", TARGET, 99)]
    assert OTHER in f.text and "not the sender" in f.text


def test_ownable_when_the_sender_is_the_owner_points_further_down():
    f = _access(_text("Ownable: caller is not the owner"), FakeReader(owner=SENDER))
    assert f.rule == "access_control" and f.level == "single_source" and "another contract" in f.text


def test_role_message_v4_is_read_with_its_account_and_role():
    reader = FakeReader(has_role=False)
    f = _access(_text(f"AccessControl: account {SENDER} is missing role {ROLE}"), reader)
    assert f.level == "confirmed" and reader.calls == [("hasRole", TARGET, ROLE, SENDER, 99)]


def test_v5_errors_name_the_refused_account():
    reader = FakeReader(owner=OTHER)
    f = _access(_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]), reader)
    assert f.level == "confirmed"
    # the refused account is a contract in between, not the sender: no read on the target proves anything
    reader = FakeReader(owner=OTHER)
    f = _access(_custom("OwnableUnauthorizedAccount(address account)", [("account", OTHER)]), reader)
    assert f.level == "single_source" and reader.calls == [] and OTHER in f.text and "not the sender" in f.text
    reader = FakeReader(has_role=False)
    f = _access(_custom("AccessControlUnauthorizedAccount(address account, bytes32 neededRole)",
                        [("account", SENDER), ("neededRole", ROLE)]), reader)
    assert f.level == "confirmed" and reader.calls == [("hasRole", TARGET, ROLE, SENDER, 99)]


def test_access_without_a_node_is_said():
    f = _access(_text("Ownable: caller is not the owner"), None)
    assert f.rule == "access_control" and f.level == "single_source" and f.missing


# ---- review of T4 ----

def test_accept_ownership_reads_the_pending_owner():
    class Reader(FakeReader):
        def pending_owner(self, contract, block):
            self.calls.append(("pendingOwner", contract, block))
            return Read("pendingOwner()", contract, (), block, OTHER)
    reader = Reader(owner=OTHER)
    f = diagnose(_ctx(reason=_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]),
                      call={"function": "acceptOwnership", "args": {}}, sender=SENDER, to=TARGET, block=100,
                      reader=reader))
    assert reader.calls == [("pendingOwner", TARGET, 99)] and "pendingOwner()" in f.text and "pending owner" in f.text
    assert f.level == "confirmed"


def test_the_bare_v4_owner_text_is_not_confirmed_by_the_contract_called():
    f = _access(_text("Ownable: caller is not the owner"), FakeReader(owner=OTHER))
    assert f.level == "single_source" and OTHER in f.text and "further down" in f.text


def test_a_uniswap_deadline_text_with_this_calls_deadline_ahead_does_not_contradict_itself():
    f = diagnose(_ctx(reason=_text("UniswapV2Router: EXPIRED"), call={"function": "swap", "args": {"deadline": 2_000_000_000}},
                      block_time=1_791_479_507))
    assert f.rule == "deadline" and "is not this call's deadline parameter" in f.text
    assert "the transaction was included after the deadline the sender set" not in f.text


def test_a_passed_deadline_without_a_reason_keeps_the_other_finding_and_the_replay():
    no_data = RevertReason(method_call=None, parameters=(), raw_data="0x", original={"raw": "0x"})
    f = diagnose(_ctx(reason=no_data, call={"function": "swap", "args": {"deadline": 5}}, block_time=1_791_479_507,
                      gas_used=99, gas_limit=100))
    assert f.rule == "possibly_out_of_gas" and "deadline parameter is 5" in f.text and "had passed" in f.text
    f = diagnose(_ctx(reason=no_data, call={"function": "swap", "args": {"deadline": 5}}, block_time=1_791_479_507))
    assert f.rule == "no_reason" and "had passed" in f.text


def test_another_time_word_with_a_passed_deadline_stays_a_candidate():
    f = diagnose(_ctx(reason=_text("Deadline not reached"), call={"function": "f", "args": {"deadline": 5}},
                      block_time=1_791_479_507))
    assert f.rule == "deadline" and f.level == "candidate"


def test_a_deadline_equal_to_the_block_time_is_not_before_it():
    f = diagnose(_ctx(reason=_text("UniswapV2Router: EXPIRED"), call={"function": "swap", "args": {"deadline": 100}},
                      block_time=100))
    assert "is not before the block's time" in f.text


@pytest.mark.parametrize("role", ["9f" * 32, "0x9f", "0xzz" + "9f" * 31])
def test_a_role_that_is_not_32_bytes_of_hex_is_not_read(role):
    reader = FakeReader(has_role=False)
    f = _access(_custom("AccessControlUnauthorizedAccount(address account, bytes32 neededRole)",
                        [("account", SENDER), ("neededRole", role)]), reader)
    assert reader.calls == [] and f.level == "single_source" and f.missing


def test_a_v5_error_without_its_parameters_says_so():
    reader = FakeReader(has_role=False)
    f = _access(_custom("AccessControlUnauthorizedAccount(address a, bytes32 r)", [("a", SENDER)]), reader)
    assert reader.calls == [] and f.level == "single_source" and "None" not in f.text
    assert "did not give" in f.missing[0].why


def test_an_owner_read_that_is_not_an_address_is_not_held_permission():
    reader = FakeReader(owner=None)
    f = _access(_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]), reader)
    assert f.level == "single_source" and "held" not in f.text and f.missing


# ---- PHASE3 T5: out of gas, from a real Ethereum failure (USDT transfer, 60000 of 60000 gas, block 26150083) ----

def test_out_of_gas_is_its_own_rule_not_a_contract_message():
    b = _bundle("eth_fail_out_of_gas")
    finding = _finding(b)
    assert finding.data["rule"] == "out_of_gas" and finding.data["label"] == "CONFIRMED"
    assert "used all 60000 of its 60000 gas limit" in finding.text
    assert "refused the operation with its own message" not in finding.text
    assert "gas" in " ".join(finding.data["next_steps"]["support"]).lower()


def test_out_of_gas_with_gas_left_is_only_likely():
    ctx = _ctx(explorer_text="out of gas", gas_used=40000, gas_limit=60000)
    finding = diagnose(ctx)
    assert finding.rule == "out_of_gas" and finding.level == "candidate" and "inner call" in finding.text


# ---- the router could not pull the input token (real Uniswap V2 failure, block 26149987) ----

def test_a_router_pull_failure_without_reads_is_likely():
    # the real case turned out to be an inner out of gas (above); the rule alone, with no node, stays LIKELY
    f = diagnose(_ctx(reason=_text("TransferHelper: TRANSFER_FROM_FAILED"),
                      call={"function": "swapExactTokensForETH", "args": {"amountIn": "1000", "path": f"[{TARGET}]"}},
                      sender=SENDER, to=OTHER, block=100, reader=None))
    assert (f.rule, f.level) == ("router_transfer_from", "candidate") and f.missing


class AllowanceReader(FakeReader):
    def __init__(self, allowance, balance):
        super().__init__()
        self.values = {"allowance": allowance, "balance": balance}

    def erc20_allowance(self, token, owner, spender, block):
        return Read("allowance(address,address)", token, (owner, spender), block, self.values["allowance"])

    def erc20_balance(self, token, holder, block):
        return Read("balanceOf(address)", token, (holder,), block, self.values["balance"])


@pytest.mark.parametrize("allowance, balance, rule, level", [
    (0, 10**18, "insufficient_allowance", "confirmed"),
    (10**18, 5, "insufficient_balance", "confirmed"),
    (10**18, 10**18, "router_transfer_from", "candidate"),
])
def test_a_router_pull_failure_with_reads_names_its_cause(allowance, balance, rule, level):
    call = {"function": "swapExactTokensForETH", "args": {"amountIn": "1000", "path": f"[{TARGET}, {OTHER}]"}}
    f = diagnose(_ctx(reason=_text("TransferHelper: TRANSFER_FROM_FAILED"), call=call, sender=SENDER, to=OTHER,
                      block=100, reader=AllowanceReader(allowance, balance)))
    assert (f.rule, f.level) == (rule, level)


# ---- review of T5: the inner out of gas behind TRANSFER_FROM_FAILED, and the node's receipt ----

def test_an_inner_out_of_gas_explains_the_routers_failure():
    # the real recording: the tax token's transferFrom calls back into the router, whose call to WETH9 runs out of gas
    finding = _finding(_bundle("eth_fail_transfer_from"))
    assert finding.data["rule"] == "out_of_gas" and finding.data["label"] == "LIKELY"
    assert "WETH9" in finding.text and "'out of gas'" in finding.text and "168382 of its 172298" in finding.text
    assert "approved" not in " ".join(finding.data["next_steps"]["support"])


def test_out_of_gas_is_confirmed_by_the_nodes_receipt():
    b = _bundle("eth_fail_out_of_gas")
    finding = _finding(b)
    assert finding.data["label"] == "CONFIRMED" and "the node's receipt" in finding.text
    assert any(s.kind == "rpc" for s in finding.sources)


@pytest.mark.parametrize("text", ["out of gas: not enough gas for reentrancy sentry", "Out Of Gas", "OutOfGas"])
def test_out_of_gas_spellings(text):
    assert diagnose(_ctx(explorer_text=text, gas_used=None, gas_limit=None)).rule == "out_of_gas"
    assert "None" not in diagnose(_ctx(explorer_text=text, gas_used=None, gas_limit=None)).text


# ---- D51: the failure's origin first, and every failure signal accounted for ----

def test_another_execution_error_inside_is_the_origin():
    ctx = _ctx(reason=_text("TransferHelper: TRANSFER_FROM_FAILED"),
               inner_failures=[("E5", "Parent reverted", "Internal call from A to B"),
                               ("E6", "invalid opcode", "Internal call from B to C")])
    f = diagnose(ctx)
    assert f.rule == "inner_execution_error" and f.level == "candidate"
    assert "'invalid opcode'" in f.text and "E6" in f.text and "where the failure began" in f.text


def test_propagating_errors_are_not_an_origin():
    ctx = _ctx(reason=_text("UniswapV2Router: EXPIRED"), call={"function": "swap", "args": {"deadline": 5}},
               block_time=1_791_479_507, inner_failures=[("E5", "Reverted", "Internal call"),
                                                         ("E6", "Parent reverted", "Internal call")])
    f = diagnose(ctx)
    assert f.rule == "deadline" and "not explained" not in f.text


def test_a_confirmed_cause_that_leaves_a_signal_unexplained_is_likely():
    reader = FakeReader(owner=OTHER)
    ctx = _ctx(reason=_custom("OwnableUnauthorizedAccount(address account)", [("account", SENDER)]),
               sender=SENDER, to=TARGET, block=100, reader=reader,
               inner_failures=[("E9", "out of gas", "Internal call from X to Y")])
    f = diagnose(ctx)
    assert f.rule == "access_control" and f.label == "LIKELY"
    assert "Also present, and not explained by this conclusion" in f.text and "E9" in f.text


def test_all_gas_used_next_to_another_reason_is_said():
    ctx = _ctx(reason=_text("Pausable: paused"), gas_used=99_500, gas_limit=100_000)
    f = diagnose(ctx)
    assert f.rule == "paused" and "used 99500 of its 100000 gas limit" in f.text and f.label == "LIKELY"


def test_a_conclusion_that_explains_the_signals_keeps_its_label():
    b = _bundle("eth_fail_out_of_gas")  # all gas used, explained by the out-of-gas conclusion itself
    finding = _finding(b)
    assert finding.data["label"] == "CONFIRMED" and "not explained" not in finding.text


def test_the_signal_check_never_raises_a_label():
    ctx = _ctx(generic_failure="zkSync's generic failure text", inner_failures=[("E3", "out of gas", "Internal call")])
    f = diagnose(ctx)
    assert label_for(f.rule, f.level, f.label) == "UNKNOWN" and "not explained" in f.text
