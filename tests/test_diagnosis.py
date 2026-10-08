"""PHASE2 T4 rules: cases from the clean-context review, written before the fixes."""
import pytest

from anychain.collectors.http import CollectorError
from anychain.collectors.types import RevertReason
from anychain.diagnosis import Context, diagnose
from anychain.reads import Read, UnreadableState


def _reason(text):
    return RevertReason.from_api({"method_call": "Error(string reason)", "parameters": [{"name": "reason", "value": text}]})


def _ctx(reason=None, result="Reverted", call=None, reader=None, gas=(1, 10), **kw):
    return Context(reason=reason, result=result, call=call, sender=kw.get("sender", "0x" + "a" * 40),
                   to=kw.get("to", "0x" + "b" * 40), to_text="the token", block=100, gas_used=gas[0], gas_limit=gas[1],
                   reader=reader, explorer_text=kw.get("explorer_text"))


class Reader:
    """Answers reads with fixed values, or raises what it is given."""

    def __init__(self, value=None, error=None):
        self.value, self.error, self.asked = value, error, []

    def _answer(self, signature, contract, args, block):
        self.asked.append((signature, args, block))
        if self.error:
            raise self.error
        return Read(signature, contract, args, block, self.value)

    def erc20_balance(self, token, holder, block):
        return self._answer("balanceOf(address)", token, (holder,), block)

    def erc20_allowance(self, token, owner, spender, block):
        return self._answer("allowance(address,address)", token, (owner, spender), block)

    def paused(self, contract, block):
        return self._answer("paused()", contract, (), block)


NO_DATA = RevertReason.from_api({"raw": "0x"})


def test_execution_reverted_without_data_is_no_reason_not_a_contract_reason():
    # Celo/Optimism explorers put "execution reverted" in `result` for a data-less revert (real case recorded).
    f = diagnose(_ctx(NO_DATA, result="execution reverted", explorer_text="execution reverted"))
    assert f.rule == "no_reason"


@pytest.mark.parametrize("text", ["Price too old", "Signature expired", "Order expired", "Deadline not reached",
                                  "Auction deadline has not passed", "block number deadline"])
def test_words_about_time_are_only_a_candidate(text):
    f = diagnose(_ctx(_reason(text)))
    assert f.level == "candidate" and "Send it again with a later deadline" not in " ".join(f.next_steps)


@pytest.mark.parametrize("text", ["Transaction too old", "UniswapV2Router: EXPIRED"])
def test_known_deadline_checks_are_stated(text):
    f = diagnose(_ctx(_reason(text)))
    assert (f.rule, f.level) == ("deadline", "single_source") and f.source_urls


def test_enough_balance_at_the_parent_block_does_not_guess_one_cause():
    call = {"function": "transfer", "args": {"to": "0x" + "c" * 40, "amount": 50}}
    f = diagnose(_ctx(_reason("ERC20: transfer amount exceeds balance"), call=call, reader=Reader(value=80)))
    assert f.level == "single_source" and "probably" not in f.text
    assert "transfer fee" in f.text and "another transfer" in f.text  # other causes are named


def test_balance_without_a_read_does_not_blame_the_sender():
    f = diagnose(_ctx(_reason("ERC20: transfer amount exceeds balance"), call={"function": "multicall", "args": {}}))
    assert "the sender held" not in f.text and "the account the tokens were moved from" in f.text


def test_paused_false_on_the_target_names_a_nested_contract():
    f = diagnose(_ctx(_reason("paused"), reader=Reader(value=False)))
    assert f.level == "single_source" and "another contract" in f.text


def test_paused_confirmed_says_what_was_read_not_who_reverted():
    f = diagnose(_ctx(_reason("paused"), reader=Reader(value=True)))
    assert f.level == "confirmed" and "and it reverted" not in f.text


def test_slippage_next_step_warns_about_worse_prices():
    f = diagnose(_ctx(_reason("Too little received"), call={"function": "exactInputSingle", "args": {}}))
    assert any("sandwich" in s for s in f.next_steps) and f.level == "single_source"


def test_a_function_named_swap_something_is_not_a_router_swap():
    f = diagnose(_ctx(_reason("Too little received"), call={"function": "swapOwner", "args": {}}))
    assert f.level == "candidate"


def test_allowance_is_read_with_owner_and_the_caller_as_spender():
    owner, sender = "0x" + "d" * 40, "0x" + "a" * 40
    call = {"function": "transferFrom", "args": {"from": owner, "to": "0x" + "c" * 40, "amount": 100}}
    reader = Reader(value=40)
    f = diagnose(_ctx(_reason("ERC20: transfer amount exceeds allowance"), call=call, reader=reader, sender=sender))
    assert f.level == "confirmed" and reader.asked == [("allowance(address,address)", (owner, sender), 99)]


def test_all_gas_candidate_names_other_causes_and_a_safe_next_step():
    f = diagnose(_ctx(NO_DATA, gas=(9850, 10000)))  # 98.5 %: a sub-call out of gas leaves the caller 1/64
    assert f.rule == "possibly_out_of_gas"
    assert "assert" in f.text and any("estimate" in s.lower() and "do not" in s.lower() for s in f.next_steps)


@pytest.mark.parametrize("error, cause", [
    (UnreadableState("paused() on X answered 0 bytes"), "not_interpretable"),
    (CollectorError("timeout", retryable=True), "source_unavailable"),
    (CollectorError("Archive requests require a personal token", retryable=False), "source_error"),
])
def test_a_read_that_fails_names_the_right_cause(error, cause):
    f = diagnose(_ctx(_reason("paused"), reader=Reader(error=error)))
    [missing] = f.missing
    assert missing.cause == cause
