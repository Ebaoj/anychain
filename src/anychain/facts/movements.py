"""What moved: token transfers, native movements, internal calls and events. Part of BundleBuilder (bundle.py), split
by concern (D79).
"""
from anychain.collectors.explorer import MAX_PAGES
from anychain.collectors.types import AddressRef, InternalCall, Log, TokenTransfer, Transaction, to_int
from anychain.models import Source
from anychain.facts.common import (MAX_EVENTS, MAX_EVENT_LOOKUPS, MAX_INTERNAL, UnreadableLog, VALUE_IS_CONTEXT,
    _failure_note, address_of, amount)


class MovementFacts:

    def _add_transfers(self, tx_hash: str, tx: Transaction) -> set[int]:
        """Token transfers. Returns the log indexes they cover, so events skip them."""
        transfers, truncated = self.explorer.token_transfers(tx_hash)
        source = self._api_source(f"/transactions/{tx_hash}/token-transfers", "Explorer API: token transfers")
        covered: set[int] = {t.log_index for t in transfers if t.log_index is not None}
        native = self.cfg.network.native_token_contract
        native_moves = [t for t in transfers if native and (t.token.address or "").lower() == native]
        explorer_fee = self.profile.fee(tx.raw)
        others = [t for t in transfers if t not in native_moves]
        self._add_native_movements(native_moves, address_of(tx.sender),
                                   explorer_fee.total_raw if explorer_fee else None, others, source)
        for t in [t for t in transfers if t not in native_moves]:
            text = f"Token transfer: {self._transfer_what(t)} " \
                   f"from {self._party(t.sender)} to {self._party(t.recipient)}."
            self.bundle.add("token_transfer", text, [source],
                            {"token": t.token.address, "from": address_of(t.sender), "to": address_of(t.recipient),
                             "value": t.value, "token_id": t.token_id})
        if truncated:
            self._gap("Token transfers", f"more than {MAX_PAGES} pages of transfers",
                      "Open the explorer page for the full list", retryable=False, cause="not_interpretable")
        return covered

    def _remember_movement(self, t: TokenTransfer, fact_id: str) -> None:
        key = ((address_of(t.sender) or "").lower(), (address_of(t.recipient) or "").lower(), t.value or 0)
        self.native_movement_facts.setdefault(key, fact_id)

    def _add_native_movements(self, moves: list[TokenTransfer], sender: str | None, explorer_fee: int | None,
                              other_transfers: list[TokenTransfer], source: Source) -> None:
        """Transfers of the native-token contract are the native currency moving, not a second asset.
        When the network collects fees through a visible address, those moves are the fee flow."""
        if not moves:
            return
        sym, collector = self.cfg.network.native_symbol, self.cfg.network.fee_collector
        def is_collector(ref: AddressRef | None) -> bool:
            return bool(collector) and (address_of(ref) or "").lower() == collector
        to_collector = [t for t in moves if is_collector(t.recipient)]
        from_collector = [t for t in moves if is_collector(t.sender)]
        for t in moves:
            if t in to_collector or t in from_collector:
                continue
            fact = self.bundle.add("native_transfer", f"Native {sym} movement of {self._native_amount(t.value)} from "
                                   f"{self._party(t.sender)} to {self._party(t.recipient)} (the explorer also lists it "
                                   f"as a token transfer of the native-token contract; it is not a second asset).",
                                   [source], {"from": address_of(t.sender), "to": address_of(t.recipient),
                                              "value": t.value})
            self._remember_movement(t, fact.id)
        if to_collector:
            flow = self._add_fee_flow(to_collector, from_collector, sender, explorer_fee, other_transfers, source)
            for t in to_collector + from_collector:
                self._remember_movement(t, flow)

    def _transfer_what(self, t: TokenTransfer) -> str:
        """'69.3484 USDC', '50 x NAME token #7', 'NAME token #443098', or the raw amount."""
        symbol = t.token.label
        if t.token_id is not None and t.value is not None:
            return f"{t.value} x {symbol} token #{t.token_id}"  # ERC-1155: an id and a quantity
        if t.token_id is not None:
            return f"{symbol} token #{t.token_id}"  # ERC-721: one NFT
        if t.value is not None and t.decimals is not None:
            return f"{amount(t.value, t.decimals)} {symbol}"
        if t.value is not None:
            return f"{t.value} raw units of {symbol} (decimals unknown)"
        return f"{symbol} (amount not reported by the explorer)"

    def _add_internal(self, tx_hash: str, tx: Transaction) -> None:
        if tx.result == "awaiting_internal_transactions":
            self._gap("Internal calls", "the explorer is still indexing this transaction's internal calls",
                      "Ask again in a few minutes", retryable=True, cause="source_behind")
            return
        items, truncated = self.explorer.internal_transactions(tx_hash)
        source = self._api_source(f"/transactions/{tx_hash}/internal-transactions", "Explorer API: internal transactions")
        reads = [it for it in items if it.type == "staticcall"]
        system = [it for it in items if it.type != "staticcall" and self._between_system_contracts(it)]
        calls = [it for it in items if it.type != "staticcall" and it not in system]
        # The cutoff never drops a call that carried value: those first, the rest fill the room left,
        # all in the explorer's order (real tx 0xd58d0906...: the 3 value calls were at 48, 70, 95).
        movers = {i for i, it in enumerate(calls) if it.value > 0 and it.type not in VALUE_IS_CONTEXT}
        room = max(MAX_INTERNAL - len(movers), 0)
        kept = movers | set([i for i in range(len(calls)) if i not in movers][:room])
        shown = [it for i, it in enumerate(calls) if i in kept]
        for it in shown:
            text, moves_value = self._internal_text(it)
            same_as = self._same_native_movement(it)
            if same_as:
                text = text.rstrip(".") + f": the same movement as {same_as}, not an additional one."
            self.bundle.add("internal_call", text, [source],
                            {"type": it.type, "value": str(it.value), "moves_value": moves_value, "same_as": same_as,
                             **({"error": it.error} if it.error else {})})
        if reads:
            self.bundle.add("internal_call", f"{len(reads)} read-only staticcall(s) (no state change) not listed.",
                            [source], {"staticcalls": len(reads)})
        if system:
            moved = sum(it.value for it in system)
            text = f"{len(system)} internal call(s) between the network's system contracts not listed"
            text += f" (they carry {self._native_amount(moved)} in total)." if moved else " (no value carried)."
            self.bundle.add("internal_call", text, [source], {"system_calls": len(system), "value": str(moved)})
        if len(shown) < len(calls):
            scope = " outside the system-contract group" if system and any(it.value for it in system) else ""
            self._gap("Internal calls", f"showing {len(shown)} of {len(calls)} internal calls; every internal call"
                      f"{scope} that carried {self.cfg.network.native_symbol} is listed"
                      + (" (among those fetched)" if truncated else ""),
                      "Open the explorer page for the full list", retryable=False, cause="not_interpretable")
        if truncated:
            self._gap("Internal calls", f"the explorer's list was cut after {MAX_PAGES} pages, so later internal "
                      "calls (and any value they carried) are not known", "Open the explorer page for the full list",
                      retryable=False, cause="not_interpretable")

    def _same_native_movement(self, it: InternalCall) -> str | None:
        """Fact id of a native movement already stated with the same sender, recipient and value."""
        if it.type != "call" or it.value <= 0 or it.success is False:
            return None
        key = ((address_of(it.sender) or "").lower(), (address_of(it.recipient) or "").lower(), it.value)
        return self.native_movement_facts.get(key)

    def _between_system_contracts(self, it: InternalCall) -> bool:
        """Both ends are system contracts (addresses up to network.system_address_max, e.g. zkSync's)."""
        limit = self.cfg.network.system_address_max
        if limit is None:
            return False
        ends = [to_int(address_of(it.sender)), to_int(address_of(it.recipient))]
        return all(n is not None and 0 < n <= limit for n in ends)

    def _internal_text(self, it: InternalCall) -> tuple[str, bool | None]:
        """Describe one internal call, and say whether native value really moved (None: not known)."""
        sym, kind, value = self.cfg.network.native_symbol, it.type, it.value
        shown = f"{amount(value, self.cfg.network.native_decimals)} {sym}"
        ok = it.success is not False

        if kind in ("create", "create2"):
            creator = self._party(it.sender)
            if it.success is None:
                return f"Internal {kind} by {creator}; the explorer does not say if the deployment succeeded.", None
            if not ok:
                return f"Internal {kind} by {creator} ({_failure_note(it, short=True)}): nothing was deployed or " \
                       "transferred.", False
            deployed = (self._party(it.created_contract) if it.created_contract
                        else "a contract whose address the explorer does not report")
            text = f"Internal {kind} by {creator}: deployed {deployed}"
            return (f"{text} with {shown}." if value > 0 else f"{text}."), value > 0

        route = f"from {self._party(it.sender)} to {self._party(it.recipient)}"
        if kind == "call" and value > 0:
            if it.success is None:
                return f"Internal call {route} with {shown} attached; the explorer does not say if it succeeded.", None
            if ok:
                return f"Internal {sym} transfer of {shown} {route}.", True
            return (f"Internal call {route} tried to send {shown} {_failure_note(it)}; nothing was transferred.",
                    False)

        text = f"Internal {kind} {route}" + ("" if ok else f" ({_failure_note(it, short=True)})")
        if value > 0 and kind in VALUE_IS_CONTEXT:
            return f"{text}; no {sym} moved (the value is the caller's own context).", False
        if value > 0:  # e.g. selfdestruct: not yet seen in a real recording, so not interpreted
            return f"{text}; the explorer records a value of {shown} on it.", None
        return f"{text}.", False

    def _add_events(self, tx_hash: str, covered_logs: set[int]) -> None:
        items, truncated = self.explorer.logs(tx_hash)
        source = self._api_source(f"/transactions/{tx_hash}/logs", "Explorer API: logs")
        logs = [log for log in items if log.index not in covered_logs]
        if len(logs) > MAX_EVENTS:
            logs, truncated = logs[:MAX_EVENTS], True
        unreadable, failed = 0, []
        for log in logs:
            try:
                self._add_one_event(log, source)
            except UnreadableLog:
                unreadable += 1
            except Exception as exc:  # one log our code failed on must not cost the others
                failed.append(f"{type(exc).__name__}: {exc}")
        if unreadable:
            self._gap("Events", f"{unreadable} log(s) had an unexpected shape and were skipped",
                      "Check the logs on the explorer page", retryable=False, cause="source_error")
        if failed:
            self._gap("Events", f"could not process {len(failed)} log(s) ({failed[0]})",
                      "Check the logs on the explorer page", retryable=False, cause="processing_error")
        if truncated:
            self._gap("Events", f"showing the first {MAX_EVENTS}", "Open the explorer page for the full list",
                      retryable=False, cause="not_interpretable")

    def _add_one_event(self, log: Log, source: Source) -> None:
        emitter = log.emitter
        if emitter is None or emitter.address is None:
            raise UnreadableLog("log without a readable emitter address")  # counted by _add_events
        address = emitter.address
        topics = list(log.topics)
        decoder = self._decoder_for(address, list(emitter.implementations))
        event = decoder.decode_log(topics, log.data) if decoder else None
        if event:
            args = ": " + ", ".join(f"{a.name}={a.value}" for a in event.args) if event.args else ""
            origin = self.abi_origin.get(address.lower())
            if origin is None:
                self.bundle.add("event", f"Event {event.signature} emitted by {self._party(emitter)}{args}.", [source],
                                {"event": event.name, "args": {a.name: a.value for a in event.args}})
            else:
                abi_source, confidence = self._abi_provenance(address)
                text = (f"Event {event.signature} emitted by {self._party(emitter)}{args}." if origin[0] in ("repo_pinned", "repo_artifact")
                        else f"An event of {self._party(emitter)} matches {event.signature} in a configured repository; "
                             f"decoded with it{args or ' (no arguments)'}. Not confirmed: the repository was matched by "
                             "the event's signature only.")
                self.bundle.add("event", text, [source, abi_source],
                                {"event": event.name, "args": {a.name: a.value for a in event.args}},
                                confidence=confidence)
            return
        if decoder and decoder.could_be_anonymous(topics):
            # The first topic may be an argument, not a signature: do not label it "topic0".
            self.bundle.add("event", f"Event with {len(topics)} topic(s) emitted by {self._party(emitter)} matched none "
                            "(or more than one) of the anonymous events in its ABI; not decoded.", [source])
            self.anonymous_unmatched.add(address)
            return
        topic0 = topics[0] if topics else "(none)"
        self.bundle.add("event", f"Event with topic0 {topic0} emitted by {self._party(emitter)}; could not decode.", [source])
        self.undecoded_events.add(address)
        if topics and len(self.event_topics_asked) < MAX_EVENT_LOOKUPS and topic0.lower() not in self.event_topics_asked:
            self.event_topics_asked.add(topic0.lower())
            self._safely("Signature database", lambda: self._add_signature_candidates("event", topic0, None))

    def _declare_undecoded_events(self) -> None:
        """One gap per contract with undecoded events, not one per event."""
        for address in sorted(self.undecoded_events):
            self._declare_undecoded("Event decoding", address, "its events")
        for address in sorted(self.anonymous_unmatched):
            self._gap("Event decoding", f"{address} declares anonymous events (no signature topic); some of its "
                      "logs fit none or several of them", "Check those logs on the explorer page", retryable=False, cause="not_interpretable")
