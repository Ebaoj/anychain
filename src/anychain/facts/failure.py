"""A failure: the revert reason, the diagnosis, the replay on the node, and the context around it (the sender's
timeline, gas notes). Part of BundleBuilder (bundle.py), split by concern (D79).
"""
from dataclasses import replace
from anychain import gas, timeline
from anychain.collectors.http import CollectorError
from anychain.collectors.rpc import InsufficientFunds
from anychain.collectors.types import RevertReason, Transaction, to_int
from anychain.decoder import decode_revert
from anychain.diagnosis import Context, SUPPORT_STEPS, diagnose, label_for, steps_text
from anychain.models import Source
from anychain.reads import REPLAY_LIMITS, StateReader
from anychain.facts.common import (DEPOSIT_TX_TYPES, REPLAY_MEANING, REPLAY_WHEN, RESULT_WITHOUT_REASON,
    TIMELINE_PAGES, _unix, address_of)


class FailureFacts:

    def _add_revert(self, tx_hash: str, tx: Transaction) -> None:
        if self.bundle.status != "failed":
            return
        source = self._tx_source(tx_hash)
        reason, result = tx.revert_reason, tx.result
        if reason and reason.carried_no_data:
            self.bundle.add("revert", "The transaction reverted without any revert data: no reason text and no "
                            "custom error (e.g. a bare revert(), a failed assert in old Solidity, or a call to "
                            "code that does not exist).", [source], {"revert_reason": reason.original})
            return
        if reason and reason.reported:
            self.bundle.add("revert", f"Explorer reports the revert reason: {reason.describe()}.",
                            [source], {"revert_reason": reason.original})
        elif isinstance(result, str) and result not in RESULT_WITHOUT_REASON:
            self.bundle.add("revert", f"Explorer reports the failure as: {result!r}.", [source], {"result": result})
        else:
            self._gap("Revert reason", "the explorer did not report why the transaction failed",
                      "A node that can re-execute or trace the call (debug/trace RPC)", retryable=False, cause="not_interpretable")

    def _add_diagnosis(self, tx_hash: str, tx: Transaction) -> None:
        """A failed transaction's likely cause, confirmed by a state read when one can (PHASE2 T4)."""
        if self.bundle.status != "failed":
            return
        call = next((e for e in self.bundle.items if e.kind == "call" and e.data.get("function")), None)
        generic = self.profile.generic_failure(tx.result if isinstance(tx.result, str) else None)
        explorer_text = tx.result if isinstance(tx.result, str) and tx.result not in RESULT_WITHOUT_REASON else None
        ctx = Context(reason=tx.revert_reason, result=tx.result if isinstance(tx.result, str) else None,
                      call=call.data if call else None, sender=address_of(tx.sender), to=address_of(tx.to),
                      to_text=self._party(tx.to), block=tx.block_number, gas_used=tx.gas_used, gas_limit=tx.gas_limit,
                      reader=StateReader(self.rpc) if self.rpc_verified else None,
                      generic_failure=generic[0] if generic else None,
                      explorer_text=None if generic else explorer_text, block_time=_unix(tx.timestamp),
                      inner_failures=[(e.id, e.data["error"], e.text.split(" (this internal call failed")[0])
                                      for e in self.bundle.items if e.kind == "internal_call" and e.data.get("error")],
                      node_gas=self._node_gas())
        finding = diagnose(ctx)
        read_ids = []
        for read in finding.reads:
            fact = self.bundle.add("state_read", f"At block {read.block}, {read.signature.split('(')[0]} on "
                                   f"{read.contract} returned {read.value}.",
                                   [self._rpc_source(read.detail)], {"call": read.detail, "value": str(read.value)})
            read_ids.append(fact.id)
        sources = [self._tx_source(tx_hash)] + [self._rpc_source(r.detail) for r in finding.reads]
        if finding.data.get("node_gas"):  # the node's receipt and transaction agree on the gas (D50)
            sources.append(self._rpc_source(f"eth_getTransactionReceipt {tx_hash} gasUsed, eth_getTransactionByHash gas"))
        if generic:
            sources.append(Source(kind="repo", label="Network software source", url=generic[1]))
        sources += [Source(kind="repo", label="Source of the rule's meaning", url=u) for u in finding.source_urls]
        label = label_for(finding.rule, finding.level, finding.label)
        self.bundle.add("diagnosis", f"{label}: {finding.text} {steps_text(finding.rule, finding.next_steps)}", sources,
                        {"rule": finding.rule, "level": finding.level, "label": label, "reads": read_ids,
                         "next_steps": {"support": SUPPORT_STEPS[finding.rule], "developer": finding.next_steps},
                         "computed": finding.data},
                        confidence="confirmed" if finding.level == "confirmed" else finding.level)
        for missing in finding.missing:
            self._gap("Diagnosis", missing.why, missing.needed, retryable=missing.retryable, cause=missing.cause)
        if finding.rule in REPLAY_WHEN and ctx.reader is not None:
            self._add_replay(tx_hash, tx, ctx)

    def _add_gas_notes(self, tx: Transaction) -> None:
        """Gas notes (PHASE4 T3, R3, D57): the share of the limit used, the same call's gas when the sender's
        timeline has a success of it, and storage used inside a loop of the called function's verified code."""
        texts, sources = [], [self._tx_source(self.bundle.tx_hash)]
        usage = gas.usage_note(tx.gas_used, tx.gas_limit, self.bundle.status)
        if usage:
            texts.append(usage)
        timeline_fact = next((e for e in self.bundle.items if e.kind == "timeline" and e.data.get("rows")), None)
        same = gas.same_call_note(timeline_fact) if timeline_fact else None
        if same:
            texts.append(same)
            sources += timeline_fact.sources
        code = next((e for e in self.bundle.items if e.kind == "code"), None)
        if self.called_code and code is not None:
            verified, found, name, address = self.called_code
            loop = gas.storage_in_loop(verified, found, name)
            if loop:
                texts.append(f"In the called function's code ({code.id}), heuristic: "
                             + "; ".join(t for _n, t in loop) + ".")
                sources.append(self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source"))
        if texts:
            self.bundle.add("gas_note", "Gas notes (heuristic, not a finding): " + " ".join(texts), sources,
                            {"used": tx.gas_used, "limit": tx.gas_limit})

    def _add_timeline(self, tx_hash: str, tx: Transaction) -> None:
        """The sender's transactions around this failure and the patterns they show (PHASE4 T2, R2, D56). Bounded:
        the latest pages of the sender's sent transactions until this block is reached (TIMELINE_PAGES at most),
        plus one page from this block back."""
        sender, block = address_of(tx.sender), tx.block_number
        if not sender or block is None:
            return
        rows, nxt = self.explorer.sent_page(sender)
        for _ in range(TIMELINE_PAGES - 1):
            if not nxt or any(r.block is not None and r.block <= block for r in rows):
                break
            more, nxt = self.explorer.sent_page(sender, after=nxt)
            rows += more
        reached = any(r.block is not None and r.block <= block for r in rows)
        before, _ = self.explorer.sent_page(sender, before_block=block + 1)
        seen = {r.hash for r in rows}
        rows += [r for r in before if r.hash not in seen]
        source = self._api_source(f"/addresses/{sender}/transactions?filter=from", "Explorer API: the sender's "
                                  "transactions")
        found = timeline.build(tx_hash, sender, rows)
        if found is None:
            self._gap("Timeline", "this transaction is not in the explorer's list of the sender's transactions",
                      "Ask again in a minute", retryable=True, cause="source_behind")
            return
        if not reached:
            self._gap("Timeline", f"the sender sent at least {len(rows) - len(before)} transactions after this one; "
                      "those right after it are not shown", "Open the sender's page on the explorer",
                      retryable=False, cause="not_interpretable")

        def line(r) -> str:
            outcome = "succeeded" if r.result == "success" else f"failed ({r.result!r})"
            mark = " (this transaction)" if r is found.failed else ""
            return f"nonce {r.nonce}: {r.method or 'a call'} to {r.to or '(contract creation)'}, {outcome}, at {r.timestamp}{mark}"
        self.bundle.add("timeline", "The sender's own transactions around this failure, by nonce: "
                        + "; ".join(line(r) for r in found.rows) + ".", [source],
                        {"rows": [{"hash": r.hash, "nonce": r.nonce, "method": r.method, "to": r.to,
                                   "result": r.result, "timestamp": r.timestamp, "gas_used": r.gas_used,
                                   "gas_limit": r.gas_limit, "this": r is found.failed} for r in found.rows]})
        for name, sentence in found.patterns:
            self.bundle.add("timeline", sentence, [source], {"pattern": name})

    def _node_gas(self) -> tuple[int, int] | None:
        """(gas used, gas limit) as the node reports them (receipt, transaction), when both are known."""
        view = self.rpc_view
        if view is None or view.receipt is None or view.tx is None:
            return None
        used, limit = getattr(view.receipt, "gas_used", None), getattr(view.tx, "gas", None)
        return (used, limit) if isinstance(used, int) and isinstance(limit, int) else None

    def _add_replay(self, tx_hash: str, tx: Transaction | None, ctx: Context) -> None:
        """No reason from the explorer: run the call again on the node at the parent block (PHASE2 T5, R2, D32).
        What the node answers is a fact about the replay; as a cause of the original failure it is a candidate."""
        skip = self._replay_skip(tx)
        if skip:
            self._gap("Replay", f"the call was not re-run: {skip}", "A node that can trace the transaction",
                      retryable=False, cause="not_interpretable")
            return
        node_tx = self.rpc_view.tx if self.rpc_view is not None else None
        if node_tx is not None and node_tx.sender and node_tx.to and node_tx.input is not None:
            # the node's own transaction: the replay's inputs then come from the node, like its answer
            sender, to, data, value, gas = node_tx.sender, node_tx.to, node_tx.input, node_tx.value or 0, node_tx.gas
            sources = [self._rpc_source(f"eth_call replay of {tx_hash} from {sender} with its gas limit")]
        elif tx is not None:
            sender, to, data, value, gas = ctx.sender, ctx.to, tx.raw_input, tx.value or 0, tx.gas_limit
            sources = [self._rpc_source(f"eth_call replay of {tx_hash} from {sender}"), self._tx_source(tx_hash)]
        else:
            sender = to = data = None
            value, gas, sources = 0, None, []
        block = tx.block_number if tx is not None else (node_tx.block_number if node_tx else None)
        if not sender or not to or data is None or block is None:
            self._gap("Replay", "the call was not re-run: its sender, target, data or block is not known",
                      "The transaction's details from the explorer or the node", retryable=False, cause="not_interpretable")
            return
        try:
            replay = ctx.reader.replay(sender, to, data, value, block - 1, gas=gas)
        except InsufficientFunds:
            self._gap("Replay", f"the call could not be re-run: at block {block - 1} the sender did not hold the value "
                      "the transaction sends (it may have received it earlier in its own block)",
                      "A node that can trace the transaction", retryable=False, cause="not_interpretable")
            return
        except CollectorError as exc:
            self._gap("Replay", f"the call could not be re-run: {exc}",
                      "Try again in a few minutes" if exc.retryable else
                      "A node that keeps state for the block before the transaction", retryable=exc.retryable,
                      cause="source_unavailable" if exc.retryable else "source_error")
            return
        if not replay.reverted:
            self.bundle.add("replay", f"Re-run on the node at block {replay.block}, the block before, with the "
                            f"transaction's own call, it did not revert. That is inconclusive: the replay is "
                            f"{REPLAY_LIMITS}.", sources, {"reverted": False, "block": replay.block})
            return
        # A custom error is decoded with the contract's own ABI: the explorer's, or a repo's when pinned in the
        # config. A repo selector match (not pinned) is never used here: a generic error name from an unrelated
        # repo would read as this contract's.
        decoder = self._decoder_for(to)
        origin = self.abi_origin.get(to.lower(), ("explorer",))[0]
        decoded = decode_revert(replay.revert_data, decoder if origin != "repo_match" else None)
        if decoded.kind == "custom" and origin in ("repo_pinned", "repo_artifact"):
            sources = sources + [self._abi_provenance(to)[0]]  # the error's meaning comes from the repo
        fact = self.bundle.add("replay", f"Re-run on the node at block {replay.block}, the block before, with the "
                               f"transaction's own call, it reverted with {decoded.text}. The replay is "
                               f"{REPLAY_LIMITS}, so this may differ from what happened in the original transaction.",
                               sources, {"reverted": True, "block": replay.block, "revert_kind": decoded.kind,
                                         "revert_data": replay.revert_data, "reason": decoded.reason})
        if decoded.kind == "unknown":
            self._gap("Replay", f"the replay's revert {decoded.selector} is a custom error the called contract's ABI "
                      "does not declare, or no ABI for it is available",
                      "The contract's ABI: a verified contract on the explorer, or a configured repo",
                      retryable=False, cause="not_interpretable")
            self._safely("Signature database", lambda: self._add_signature_candidates(
                "error", decoded.selector, replay.revert_data))
        if decoded.kind not in ("error_string", "custom", "panic"):
            return  # nothing more to say than the replay fact
        self._add_replay_finding(ctx, decoded, fact, sources)

    def _replay_skip(self, tx: Transaction | None) -> str | None:
        """Why a replay of this transaction would not reproduce its call (None: it can be tried). Read from the
        explorer's copy, or from the node's when the explorer is unavailable."""
        node_tx = self.rpc_view.tx if self.rpc_view is not None else None
        creation = (tx.to is None or not address_of(tx.to)) if tx is not None else (node_tx is None or not node_tx.to)
        tx_type = to_int(tx.raw.get("type")) if tx is not None else (node_tx.tx_type if node_tx else None)
        if creation:
            return "it is a contract creation, which a call cannot repeat"
        if tx_type in DEPOSIT_TX_TYPES:
            return ("it is a deposit from L1, whose funds the network adds as it runs; a replay cannot include "
                    "that")
        if (tx is not None and tx.authorizations) or (node_tx is not None and node_tx.delegates):
            return "it set account code (EIP-7702), which a replay cannot include"
        return None

    def _add_replay_finding(self, ctx: Context, decoded, fact, sources: list[Source]) -> None:
        """What the replayed revert would mean, worded as the replay's, never as the transaction's."""
        if decoded.kind == "panic":
            meaning, steps, urls = ("a check the compiler adds; the contract's code met that condition with these "
                                    "inputs"), ["Check the inputs that reach this condition in the contract's code."], []
        else:
            if decoded.kind == "error_string":
                reason = RevertReason.from_api({"method_call": "Error(string reason)",
                                                "parameters": [{"name": "reason", "value": decoded.reason}]})
            else:
                reason = RevertReason.from_api({"method_call": decoded.error.signature, "parameters": [
                    {"name": a.name, "value": a.value} for a in decoded.error.args]})
            finding = diagnose(replace(ctx, reason=reason, result=None, generic_failure=None, explorer_text=None,
                                       reader=None))  # no reads: they would describe the original, not the replay
            meaning = REPLAY_MEANING.get((finding.rule, finding.level)) or REPLAY_MEANING.get(finding.rule)
            if meaning is None:
                return
            steps, urls = finding.next_steps, finding.source_urls
        self.bundle.add("diagnosis", f"LIKELY: Possible cause, from the replay ({fact.id}): the replay reverted with "
                        f"{decoded.text}, {meaning}. If the original transaction failed the same way, that is its "
                        "cause. " + steps_text("replay", steps),
                        sources + [Source(kind="repo", label="Source of the rule's meaning", url=u) for u in urls],
                        {"rule": "replay", "level": "candidate", "label": "LIKELY", "from_replay": True,
                         "meaning": finding.rule if decoded.kind != "panic" else "panic",
                         "replay": fact.id, "reads": [], "next_steps": {"support": SUPPORT_STEPS["replay"],
                                                                       "developer": steps}}, confidence="candidate")
