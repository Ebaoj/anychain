"""The fee and the network's own facts: fee parts, fee tokens, fee flow and paymasters, chain-type facts. Part of
BundleBuilder (bundle.py), split by concern (D79).
"""
from anychain.chains import BLOCKSCOUT_CHAIN_TYPES, ChainGap, Fee, FeePart, TokenRef, audit_fields
from anychain.collectors.types import TokenTransfer, Transaction
from anychain.models import Source
from anychain.facts.common import address_of, amount


class FeeFacts:

    def _add_fee(self, tx_hash: str, tx: Transaction) -> None:
        fee = self._with_configured_fee_tokens(self.profile.fee(tx.raw))
        if fee is None:
            return
        text = f"Fee paid: {self._fee_text(fee)}."
        if fee.warnings:
            text += " This may not be the whole fee (see the Fee gap)."
        for warning in fee.warnings:
            self._gap("Fee", warning, "Check the fee on the explorer page", retryable=False, cause="source_error")
        for token in [p.token for p in fee.parts if p.token and p.token.decimals is None]:
            self._gap("Fee", f"the explorer does not report the decimals of the fee token {token.address}, so the "
                      "fee is shown in raw units", "The token's decimals (its contract or the explorer's token page)",
                      retryable=False, cause="not_interpretable")
        if self.bundle.status == "failed":
            text += " The fee is charged even though the transaction failed."
        self.bundle.add("fee", text, [self._tx_source(tx_hash)], {
            "fee": self._fee_amount(fee.total_raw, fee.single_token),
            "token": fee.single_token.symbol if fee.single_token else self.cfg.network.native_symbol,
            "parts": {p.label: self._fee_amount(p.raw, p.token) for p in fee.parts},
        })

    def _with_configured_fee_tokens(self, fee: Fee | None) -> Fee | None:
        """Describe fee tokens the explorer could not (e.g. Celo adapters), from config `fee_tokens`."""
        if fee is None:
            return None
        parts, notes = [], [fee.note] if fee.note else []
        for part in fee.parts:
            known = self.cfg.fee_tokens.get((part.token.address or "").lower()) if part.token else None
            if known is None:
                parts.append(part)
            else:
                parts.append(FeePart(part.label, part.raw, TokenRef(part.token.address, known.symbol, known.decimals)))
                notes += [known.note] if known.note else []
        return Fee(parts, note=", ".join(notes) or None, warnings=fee.warnings)

    def _fee_amount(self, raw: int, token: TokenRef | None) -> str:
        """Decimal amount; raw integer units when the token's decimals are unknown."""
        if token is not None and token.decimals is None:
            return str(raw)
        return amount(raw, token.decimals if token else self.cfg.network.native_decimals)

    def _fee_text(self, fee: Fee) -> str:
        """'0.0001 ETH', '0.0001 ETH in total: 0.00008 ETH L2 execution + 0.00002 ETH L1 data', or a token fee."""
        def money(raw: int, token: TokenRef | None) -> str:
            if token is not None and token.decimals is None:
                return f"{raw} raw units of {token.symbol}"
            return f"{self._fee_amount(raw, token)} {token.symbol if token else self.cfg.network.native_symbol}"
        token = fee.single_token
        native = self.cfg.network.native_symbol
        if len(fee.parts) == 1:
            text = money(fee.total_raw, token)
            if token is not None:
                text += f", paid in {token.symbol} instead of {native}"
        elif token is not None or all(p.token is None for p in fee.parts):
            parts = " + ".join(f"{money(p.raw, p.token)} {p.label}" for p in fee.parts)
            text = f"{money(fee.total_raw, token)} in total: {parts}"
        else:
            text = " + ".join(f"{money(p.raw, p.token)} {p.label}" for p in fee.parts)
        return f"{text} {fee.note}" if fee.note else text

    def _add_node_chain_facts(self, tx_hash: str, tx: Transaction) -> None:
        if not self.rpc_verified:
            return  # the node's chain was not confirmed (wrong chain, or the check failed): nothing it says counts
        for item in self.profile.node_facts(tx_hash, tx.raw, self.rpc.call):
            if isinstance(item, ChainGap):
                self._gap(item.what, item.why, item.needed, retryable=item.cause == "source_behind", cause=item.cause)
            else:  # the node's view compared with the explorer's: cites both, confirmed by the node
                self.bundle.add(item.kind, item.text, [self._rpc_source(item.rpc_method or "node"),
                                                       self._tx_source(tx_hash)], item.data, confidence="confirmed")

    def _add_chain_facts(self, tx_hash: str, tx: Transaction) -> None:
        """Facts only this network type has, plus a check that the config's chain_type fits the payload."""
        source = self._tx_source(tx_hash)
        for fact in self.profile.facts(tx.raw):
            self.bundle.add(fact.kind, fact.text, [source], fact.data)
        self._safely("L1 status from the node", lambda: self._add_node_chain_facts(tx_hash, tx))
        configured = self.cfg.network.chain_type
        if configured not in BLOCKSCOUT_CHAIN_TYPES:
            self._gap("Chain type", f"chain_type {configured!r} is not a Blockscout CHAIN_TYPE value this tool knows "
                      f"(typo, or an older/newer Blockscout); the generic profile is used",
                      f"Check network.chain_type; known values: {', '.join(sorted(BLOCKSCOUT_CHAIN_TYPES))}",
                      retryable=False, cause="config_error")
        elif not self.dedicated_profile:
            self._gap("Network-specific details", f"chain_type {configured!r} has no dedicated profile yet, so only "
                      "the details common to every EVM network are interpreted",
                      "A profile for this chain type in chains.py", retryable=False, cause="not_interpretable")
        audit = audit_fields(tx.raw, self.profile)
        for other_type, names in audit.other_types.items():
            self._gap("Chain type", f"the config says chain_type {configured!r}, but the explorer reports "
                      f"fields typical of {other_type!r} ({', '.join(names)})",
                      f"Check network.chain_type; if this network is {other_type!r}, set it so these are interpreted",
                      retryable=False, cause="config_error")
        if audit.unknown:
            self._gap("Network-specific details", f"the explorer reports data this tool does not interpret: "
                      f"{', '.join(audit.unknown)}", "A chain type profile that covers these fields",
                      retryable=False, cause="not_interpretable")

    def _add_fee_flow(self, prepaid: list[TokenTransfer], refunded: list[TokenTransfer], sender: str | None,
                      explorer_fee: int | None, other_transfers: list[TokenTransfer], source: Source) -> str:
        """zkSync-style fees: a prepay to the fee collector minus refunds. Says who paid."""
        sym = self.cfg.network.native_symbol
        paid_in = sum(t.value or 0 for t in prepaid)
        paid_out = sum(t.value or 0 for t in refunded)
        payers = sorted({address_of(t.sender) for t in prepaid if address_of(t.sender)})
        text = (f"Fee flow: {self._native_amount(paid_in)} prepaid to the fee collector by "
                f"{', '.join(self._party(p) for p in payers)}, {self._native_amount(paid_out)} refunded; net fee "
                f"{self._native_amount(paid_in - paid_out)}.")
        others = [p for p in payers if not sender or p.lower() != sender.lower()]
        if others:
            text += f" The {sym} fee was prepaid by {', '.join(others)} (a paymaster), not by the sender."
            text += self._paid_to_paymaster(sender, others, other_transfers)
        net = paid_in - paid_out
        matches = explorer_fee is not None and net == explorer_fee
        if explorer_fee is not None:
            text += " This matches the explorer's fee." if matches else \
                    f" The explorer's fee is {self._native_amount(explorer_fee)}, which does not match."
        fact = self.bundle.add("fee_flow", text, [source], {"prepaid": paid_in, "refunded": paid_out, "net": net,
                                                           "payers": payers, "paymaster": bool(others),
                                                           "matches_explorer_fee": matches})
        if explorer_fee is not None and not matches:
            self._gap("Fee", "the fee flow seen in transfers does not match the explorer's fee",
                      "Check the fee on the explorer page", retryable=False, cause="processing_error")
        return fact.id

    def _paid_to_paymaster(self, sender: str | None, paymasters: list[str], transfers: list[TokenTransfer]) -> str:
        """What the sender sent to the paymaster in tokens in this tx (net of what came back)."""
        if not sender:
            return ""
        lowered = {p.lower() for p in paymasters}
        net: dict[str, tuple[TokenTransfer, int]] = {}
        for t in transfers:
            frm, to = (address_of(t.sender) or "").lower(), (address_of(t.recipient) or "").lower()
            sign = 1 if (frm == sender.lower() and to in lowered) else -1 if (to == sender.lower() and frm in lowered) else 0
            if sign and t.value is not None and t.token_id is None:
                key = (t.token.address or "").lower()
                net[key] = (t, net.get(key, (t, 0))[1] + sign * t.value)
        paid = [(t, v) for t, v in net.values() if v > 0]
        if not paid:
            return " No token payment from the sender to the paymaster appears in this transaction."
        parts = ", ".join(f"{amount(v, t.decimals) if t.decimals is not None else v} {t.token.label}" for t, v in paid)
        return f" In this transaction the sender paid the paymaster {parts} (net of what it returned)."

    def _native_amount(self, raw: int | None) -> str:
        if raw is None:
            return "an amount the explorer does not report"
        return f"{amount(raw, self.cfg.network.native_decimals)} {self.cfg.network.native_symbol}"
