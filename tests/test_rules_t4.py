"""PHASE2_5 T4 (R5, R6): a deadline compared with the block's time, and access-control failures."""
import pytest

from anychain.config import load_config
from anychain.diagnosis import Context, diagnose
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
