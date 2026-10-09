"""When only the node answers: the facts it alone can give. Part of BundleBuilder (bundle.py), split by concern
(D79).
"""
from eth_utils import to_checksum_address
from anychain.collectors.http import CollectorError
from anychain.diagnosis import Context
from anychain.models import Source
from anychain.reads import StateReader
from anychain.facts.common import RpcView, _source_cause, amount


class RpcOnlyFacts:

    # ---- rpc path ----------------------------------------------------------

    def _add_rpc_only(self, tx_hash: str, view: RpcView) -> None:
        b, tx, receipt = self.bundle, view.tx, view.receipt
        source = self._rpc_source(f"eth_getTransactionByHash + eth_getTransactionReceipt [{tx_hash}]")
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        value = amount(tx.value or 0, dec)

        if receipt is None and tx.block_number is not None:
            # Mined (the node names its block) but the node returned no receipt: the outcome is unknown, never
            # "pending" (real: publicnode gave tx 0xe40d5210… in block 25947205 with a null receipt, acceptance
            # 2026-10-08).
            b.status = "unknown"
            b.add("overview", f"(From RPC only) Transaction was included in block {tx.block_number}. From {tx.sender} "
                  f"to {tx.to or '(contract creation)'}. Native value sent: {value} {sym}. Its outcome is not known: "
                  "the node returned no receipt for it.", [source])
            explorer = ("the explorer does not know this hash" if self.explorer_not_found
                         else "the explorer did not answer")
            self._gap("Final outcome", f"the node knows the transaction (block {tx.block_number}) but returned no "
                      f"receipt for it, and {explorer}", "Ask again in a few minutes, or use a node "
                      "that keeps receipts for older blocks", retryable=True, cause="source_error")
            return

        if receipt is None:
            b.status = "pending"
            b.add("overview", f"(From RPC only) Transaction is pending. From {tx.sender} to "
                  f"{tx.to or '(contract creation)'}. Native value offered: {value} {sym}.", [source])
            self._gap("Final outcome", "the transaction is still pending", "Wait for it to be mined, then ask again",
                      retryable=True, cause="pending")
            return

        b.status = receipt.status
        verb = {"success": "succeeded", "failed": "failed (reverted)"}.get(b.status, "was mined (outcome not recorded)")
        target = tx.to or f"(contract creation of {receipt.contract_address})"
        if b.status == "failed":
            value_text = f"Native value attached: {value} {sym}, not transferred because the transaction reverted."
        else:
            value_text = f"Native value: {value} {sym}."
        if b.status == "unknown":
            self._gap("Outcome", "this receipt has no status field (the node's receipt format predates it)",
                      "The explorer, which infers the outcome from execution traces", retryable=False, cause="not_interpretable")
        b.add("overview",
              f"(From RPC only) Transaction {verb} in block {tx.block_number}. "
              f"From {tx.sender} to {target}. {value_text} "
              f"Gas used {receipt.gas_used} of limit {tx.gas}.",
              [source], {"status": b.status, "from": tx.sender, "to": tx.to})
        if b.status == "failed":
            # The node's receipt says only that it reverted; the reason comes from the explorer (or a trace).
            self._gap("Revert reason", "the explorer is " + ("behind" if self.explorer_answered else "unavailable")
                      + ", and the node's receipt does not carry the reason a transaction reverted",
                      "The explorer reachable and indexing this transaction, or a node that can trace it",
                      retryable=True, cause="source_behind" if self.explorer_answered else "source_unavailable")
            if self.rpc_verified:
                ctx = Context(reason=None, result=None, call=None, sender=tx.sender, to=tx.to, to_text=str(tx.to),
                              block=tx.block_number, gas_used=receipt.gas_used, gas_limit=tx.gas,
                              reader=StateReader(self.rpc))
                self._safely("Replay", lambda: self._add_replay(tx_hash, None, ctx))

        data, authorizations = tx.input, tx.delegates
        if data is None:
            self._gap("Call decoding", "the node's call data is not readable", "Check the RPC endpoint", retryable=False, cause="source_error")
            data = ""  # neither "no data" nor a call: no call fact below
        if authorizations:
            delegates = ", ".join(sorted(set(authorizations)))
            b.add("delegation", f"Transaction carries {len(authorizations)} EIP-7702 authorization(s) naming "
                  f"{delegates}; who signed them and whether they were valid is not checked without the explorer.",
                  [source], {"applied": None})
            self._gap("Code delegations", "signers and validity of EIP-7702 authorizations need the explorer",
                      "Explorer API reachable", retryable=True,
                      cause="source_behind" if self.explorer_answered else "source_unavailable")
        if tx.to and data == "0x" and authorizations and not tx.value:
            b.add("call", f"No call data and no value were sent; the transaction carries {len(authorizations)} "
                  "EIP-7702 authorization(s).", [source])
        elif tx.to and data not in ("0x", ""):
            self._add_rpc_call(to_checksum_address(tx.to), data, source)
        b.add("receipt", f"Receipt has {receipt.log_count} log(s).", [source])
        if receipt.log_count and self.explorer_answered:
            self._gap("Event decoding", "the explorer has not indexed this transaction's events yet",
                      "Ask again in a minute", retryable=True, cause="source_behind")
        elif receipt.log_count:
            self._gap("Event decoding", "explorer unavailable, so events stay undecoded", "Explorer API reachable",
                      retryable=True, cause="source_unavailable")

    def _add_rpc_call(self, to: str, data: str, source: Source) -> None:
        """Decode the call from RPC data, using the explorer's ABI when the explorer answers."""
        if self._is_native_contract(to):
            self._add_native_contract_call(self._party(to), to, data, source)
            return
        # Without the explorer there is no "is it a contract?" flag: use the node's code today,
        # the same criterion the explorer applies. Code today = a contract.
        try:
            has_code_today = self.rpc.code_at(to, "latest") not in ("0x", "")
        except CollectorError as exc:
            has_code_today = None
            self._gap("Contract check", f"could not read whether {to} has contract code: {exc}",
                      "Try again in a few minutes", exc.retryable, _source_cause(exc.retryable))
        if has_code_today is False:
            self._add_data_to_codeless(to, (len(data) - 2) // 2, False,
                                       "The node shows no contract code at this address today", source)
            return
        decoder = self._decoder_for(to)  # explorer ABI when it answered, else the configured repos
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:  # calldata from the node, its meaning from the explorer's ABI or a repo
            abi_source, confidence = self._abi_provenance(to)
            self.bundle.add("call", self._call_text(to, decoded, to), [source, abi_source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}},
                            confidence=confidence)
            return
        self.bundle.add("call", f"Called function with selector {data[:10]} on {to}; not decoded.", [source],
                        {"selector": data[:10]})
        self._safely("Signature database", lambda: self._add_signature_candidates("call", data[:10], data))
        if self.explorer_answered:
            self._declare_undecoded("Call decoding", to, f"selector {data[:10]}")
        else:
            self._gap("Call decoding", "explorer unavailable, so no ABI could be fetched",
                      "Explorer API reachable, or the contract ABI in a configured repo", retryable=True,
                      cause="source_unavailable")
