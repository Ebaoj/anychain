"""Build the evidence bundle: deterministic, no LLM. Every fact gets an id and a source.

Reading order: `build()` runs the steps; each `_add_*` method adds facts for one topic.
Each step is wrapped by `_safely()`, so an unexpected payload costs one topic
(declared as a gap), never the whole answer. Explorer and RPC answers arrive as typed
objects (collectors/types.py); network-type specifics come from a profile (chains.py).
"""
import re
from collections.abc import Callable
from dataclasses import dataclass

from eth_utils import to_checksum_address

from anychain.chains import BLOCKSCOUT_CHAIN_TYPES, ChainGap, Fee, FeePart, TokenRef, audit_fields, profile_for
from anychain.collectors.explorer import MAX_PAGES, ExplorerClient
from anychain.collectors.http import Budget, CollectorError, NotFoundError
from anychain.collectors.rpc import RpcClient
from anychain.collectors.types import (
    AddressRef, Authorization, InternalCall, Log, RpcReceipt, RpcTransaction, TokenTransfer, Transaction, to_int,
)
from anychain.config import AppConfig
from anychain.decoder import AbiDecoder
from anychain.models import EvidenceBundle, GapCause, Source

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_EVENTS = 40
MAX_INTERNAL = 30
ZERO_ADDRESS = "0x" + "0" * 40

# Internal call types whose `value` is only the caller's context, never a payment:
# delegatecall and callcode run another contract's code inside the caller, so any
# value stays with the caller; staticcall cannot carry value. (See DECISIONS D17.)
VALUE_IS_CONTEXT = {"delegatecall", "callcode", "staticcall"}

# Blockscout `status` -> our status. `status` is null while pending.
EXPLORER_STATUS = {"ok": "success", "error": "failed"}
# Blockscout `result` values that describe state, not a failure reason.
RESULT_WITHOUT_REASON = {
    "success", "pending", "awaiting_internal_transactions", "dropped/replaced", "Reverted", "error",
}


class InvalidHashError(ValueError):
    """The input is not a transaction hash."""


@dataclass(frozen=True)
class RpcView:
    """What the RPC node told us about the transaction."""

    tx: RpcTransaction
    receipt: RpcReceipt | None  # None while pending


# ---- small pure helpers ------------------------------------------------------

def amount(raw: int, decimals: int) -> str:
    """Exact decimal string for an integer amount (no float rounding)."""
    if decimals <= 0:
        return str(raw)
    whole, frac = divmod(raw, 10**decimals)
    if frac == 0:
        return str(whole)
    return f"{whole}.{str(frac).rjust(decimals, '0').rstrip('0')}"


def party(ref: AddressRef | str | None, label_for: Callable[[str], str | None] | None = None) -> str:
    """An address with its explorer name, or else the config's name for it (label_for)."""
    if ref is None or ref == "":
        return "(none)"
    if isinstance(ref, str):
        ref = AddressRef(ref)
    if ref.address is None:
        return "(unknown address)"
    name = ref.name or (label_for(ref.address) if label_for else None)
    return f"{ref.address} ({name})" if name else ref.address


def address_of(ref: AddressRef | None) -> str | None:
    return ref.address if ref else None


def with_args(args: list) -> str:
    """' with a=1, b=2' for decoded arguments, or '' when there are none."""
    return " with " + ", ".join(f"{a.name}={a.value}" for a in args) if args else ""


# ---- the builder -------------------------------------------------------------

class BundleBuilder:
    def __init__(self, cfg: AppConfig, explorer: ExplorerClient, rpc: RpcClient):
        self.cfg = cfg
        self.explorer = explorer
        self.rpc = rpc
        self.decoders: dict[str, AbiDecoder | None] = {}  # keyed by lowercase address
        self.abi_notes: dict[str, str] = {}  # where each ABI came from, lowercase address
        self.abi_lookup_failed: dict[str, bool] = {}  # lowercase address -> was the failure retryable?
        self.explorer_not_found = False
        self.explorer_answered = False  # True when the explorer responded at all (even with 404)
        self.rpc_verified = False  # True once the RPC's chain id matched the config
        self.undecoded_events: set[str] = set()
        self.anonymous_unmatched: set[str] = set()
        # (from, to, value) of native movements already stated -> the fact id that states them,
        # so an internal call carrying the same value points to it instead of counting it again
        self.native_movement_facts: dict[tuple[str, str, int], str] = {}
        self.bundle = EvidenceBundle(network=cfg.network.name, tx_hash="", status="unknown")
        self.profile, self.dedicated_profile = profile_for(cfg.network.chain_type)

    def build(self, tx_hash: str) -> EvidenceBundle:
        self.bundle = EvidenceBundle(network=self.cfg.network.name, tx_hash=tx_hash, status="unknown")
        tx = self._fetch_explorer_tx(tx_hash)
        explorer_status = self._status_from_explorer(tx) if tx is not None else None
        rpc_view = self._fetch_rpc(tx_hash, explorer_status)

        has_receipt = rpc_view is not None and rpc_view.receipt is not None
        explorer_lagging = explorer_status in ("pending", "dropped") and has_receipt
        if explorer_lagging:
            self._gap("Explorer index", f"the explorer shows this transaction as {explorer_status}, "
                      "but the RPC node already has its receipt", "Ask again in a minute for full details",
                      retryable=True, cause="source_behind")
        if self.explorer_not_found:
            self._declare_explorer_miss(has_receipt)
        if tx is not None and not explorer_lagging:
            self._describe_from_explorer(tx_hash, tx, explorer_status)
        elif rpc_view is not None:
            self._safely("RPC-only summary", lambda: self._add_rpc_only(tx_hash, rpc_view))

        if tx is not None and rpc_view is not None and not explorer_lagging:
            self._safely("RPC cross-check", lambda: self._add_cross_check(rpc_view))
        return self.bundle

    def _declare_explorer_miss(self, has_receipt: bool) -> None:
        """The explorer answered 404. If the node has the tx, the explorer is just behind."""
        if has_receipt:
            self._gap("Explorer transaction data", "the explorer has not indexed this transaction yet",
                      "Ask again in a minute for decoded details", retryable=True, cause="source_behind")
        else:
            self._gap("Explorer transaction data", "the explorer does not know this hash",
                      "Check the hash and that the config points to the right network", retryable=False, cause="not_interpretable")

    # ---- plumbing ----------------------------------------------------------

    def _safely(self, topic: str, step: Callable[[], None]) -> None:
        """Run one step; an unexpected error becomes a gap instead of a crash."""
        try:
            step()
        except CollectorError as exc:
            needed = "Try again in a few minutes" if exc.retryable else "Check this part on the explorer page"
            self._gap(topic, str(exc), needed, exc.retryable,
                      cause="source_unavailable" if exc.retryable else "source_error")
        except Exception as exc:  # unexpected payload shape, decoding edge case...
            self._gap(topic, f"could not process the data ({type(exc).__name__}: {exc})",
                      "Check this part on the explorer page", retryable=False, cause="processing_error")

    def _party(self, ref: AddressRef | str | None) -> str:
        return party(ref, self.cfg.label_for)

    def _is_native_contract(self, address: str | None) -> bool:
        return bool(address) and address.lower() in self.cfg.native_contracts

    def _gap(self, what: str, why: str, needed: str, retryable: bool, cause: GapCause) -> None:
        """Every gap names its cause (models.GapCause): the event log alerts on the problem ones."""
        self.bundle.add_gap(what, why, needed, retryable, cause)

    def _api_source(self, path: str, label: str) -> Source:
        return Source(kind="explorer_api", label=label, url=self.explorer.url(path))

    def _tx_source(self, tx_hash: str) -> Source:
        return self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")

    def _rpc_source(self, detail: str) -> Source:
        return Source(kind="rpc", label="JSON-RPC", url=self.cfg.rpc.url, detail=detail)

    # ---- fetching ----------------------------------------------------------

    def _fetch_explorer_tx(self, tx_hash: str) -> Transaction | None:
        try:
            tx = self.explorer.transaction(tx_hash)
            self.explorer_answered = True
            return tx
        except NotFoundError:
            self.explorer_answered = True
            self.explorer_not_found = True  # judged in build(): unknown hash, or not indexed yet
            return None
        except CollectorError as exc:
            self._gap("Explorer transaction data", str(exc),
                      "Explorer API reachable and indexing this transaction", exc.retryable,
                      _source_cause(exc.retryable))
            return None

    def _fetch_rpc(self, tx_hash: str, explorer_status: str | None) -> RpcView | None:
        """Transaction + receipt from the RPC node, after checking it is on the right chain."""
        try:
            chain_id = self.rpc.chain_id()
            if chain_id != self.cfg.network.chain_id:
                self._gap("RPC transaction data",
                          f"the RPC is on chain {chain_id}, but the config says "
                          f"{self.cfg.network.chain_id}; RPC data ignored",
                          "Fix rpc.url or network.chain_id in the config", retryable=False, cause="config_error")
                return None
            self.rpc_verified = True
            tx = self.rpc.transaction(tx_hash)
            receipt = self.rpc.receipt(tx_hash) if tx else None
        except CollectorError as exc:
            self._gap("RPC transaction data", str(exc), "RPC endpoint reachable", exc.retryable,
                      _source_cause(exc.retryable))
            return None
        except Exception as exc:  # unexpected payload shape
            self._gap("RPC transaction data", f"could not process the RPC answer ({type(exc).__name__}: {exc})",
                      "Check the RPC endpoint", retryable=False, cause="processing_error")
            return None
        if tx is None:
            self._explain_rpc_miss(explorer_status)
            return None
        return RpcView(tx, receipt)

    def _explain_rpc_miss(self, explorer_status: str | None) -> None:
        """The node returned null. Why depends on what the explorer knows."""
        if explorer_status is None:
            self._gap("RPC transaction data", "the RPC node does not know this hash",
                      "Check the hash and that the config points to the right network", retryable=False, cause="not_interpretable")
        elif explorer_status in ("pending", "dropped"):
            return  # not mined: the node not having it is expected
        else:
            self._gap("RPC transaction data",
                      "the RPC node returned nothing for a transaction the explorer has; it may not keep old history",
                      "An RPC endpoint with full (archive) history", retryable=False, cause="source_error")

    # ---- explorer path -----------------------------------------------------

    def _describe_from_explorer(self, tx_hash: str, tx: Transaction, status: str) -> None:
        self.bundle.status = status
        self._safely("Transaction summary", lambda: self._add_overview(tx_hash, tx))
        if self.bundle.status in ("pending", "dropped"):
            return  # nothing below is final yet
        self._safely("Fee", lambda: self._add_fee(tx_hash, tx))
        self._safely("Network-specific details", lambda: self._add_chain_facts(tx_hash, tx))
        self._safely("Revert reason", lambda: self._add_revert(tx_hash, tx))
        self._safely("Code delegations", lambda: self._add_authorizations(tx_hash, tx))
        self._safely("Call decoding", lambda: self._add_call(tx_hash, tx))
        covered_logs: set[int] = set()
        self._safely("Token transfers", lambda: covered_logs.update(self._add_transfers(tx_hash, tx)))
        self._safely("Internal calls", lambda: self._add_internal(tx_hash, tx))
        self._safely("Events", lambda: self._add_events(tx_hash, covered_logs))
        self._declare_undecoded_events()

    def _status_from_explorer(self, tx: Transaction) -> str:
        # Blockscout stores dropped transactions with status "error", so check `result` first.
        if tx.result == "dropped/replaced":
            return "dropped"
        if tx.status in EXPLORER_STATUS:
            return EXPLORER_STATUS[tx.status]
        if tx.result == "pending":
            return "pending"
        return "unknown"

    def _add_overview(self, tx_hash: str, tx: Transaction) -> None:
        b, cfg = self.bundle, self.cfg
        sources = [Source(kind="explorer_ui", label="Explorer page", url=cfg.explorer.tx_url(tx_hash)),
                   self._tx_source(tx_hash)]
        sym = cfg.network.native_symbol
        value = amount(tx.value, cfg.network.native_decimals) if tx.value is not None else "unknown"
        sender, target = self._party(tx.sender), self._target_text(tx)
        data = {"status": b.status, "from": address_of(tx.sender), "to": address_of(tx.to),
                "value": value, "tx_type": tx.raw.get("type")}

        if b.status == "pending":
            b.add("overview", f"Transaction is pending: not yet included in a block. From {sender} to {target}. "
                  f"Native value offered: {value} {sym}. Nothing is final until it is mined.", sources, data)
            self._gap("Final outcome", "the transaction is still pending",
                      "Wait for it to be mined, then ask again", retryable=True, cause="pending")
            return
        if b.status == "dropped":
            b.add("overview", f"Transaction was dropped or replaced: it was never executed. From {sender} to {target}.",
                  sources, data)
            return

        verb = {"success": "succeeded", "failed": "failed (reverted)"}.get(b.status, f"has status {b.status}")
        if b.status == "failed":
            value_text = f"Native value attached: {value} {sym}, not transferred because the transaction reverted."
        else:
            value_text = f"Native value sent: {value} {sym}."
        # The data keeps these values exactly as the explorer sent them (gas comes as text).
        data |= {"block": tx.raw.get("block_number"), "timestamp": tx.raw.get("timestamp"),
                 "gas_used": tx.raw.get("gas_used"), "gas_limit": tx.raw.get("gas_limit")}
        b.add("overview",
              f"Transaction {verb} in block {tx.block_number} at {tx.timestamp}. "
              f"From {sender} to {target}. {value_text} "
              f"Gas used {tx.gas_used} of limit {tx.gas_limit}.",
              sources, data)

    def _target_text(self, tx: Transaction) -> str:
        if tx.to:
            return self._party(tx.to)
        created = tx.created_contract
        return f"(contract creation of {self._party(created)})" if created else "(contract creation)"

    def _add_fee(self, tx_hash: str, tx: Transaction) -> None:
        fee = self._with_configured_fee_tokens(self.profile.fee(tx.raw))
        if fee is None:
            return
        text = f"Fee paid: {self._fee_text(fee)}."
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
            else:
                self.bundle.add(item.kind, item.text, [self._rpc_source(item.rpc_method or "node")], item.data)

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

    def _add_authorizations(self, tx_hash: str, tx: Transaction) -> None:
        """EIP-7702 (type 4): accounts that set or cleared the contract code they run.

        Only an authorization the explorer marks "ok" took effect, and when one account
        has several valid ones in the same tx, the last one wins (EIP-7702 order).
        """
        source = self._tx_source(tx_hash)
        if not tx.authorizations_readable:
            self._gap("Code delegations", "the explorer's EIP-7702 authorization list is not readable",
                      "Check the authorizations on the explorer page", retryable=False, cause="source_error")
        auths = tx.authorizations
        last_valid = {str(a.authority).lower(): n for n, a in enumerate(auths) if a.status == "ok"}
        for n, auth in enumerate(auths):
            text, applied, superseded = self._authorization_text(auth, last_valid.get(str(auth.authority).lower()) != n)
            self.bundle.add("delegation", text, [source],
                            {"authority": auth.authority, "delegate": auth.delegate, "status": auth.status,
                             "applied": applied, "superseded": superseded})

    def _authorization_text(self, auth: Authorization, not_last_valid: bool) -> tuple[str, bool | None, bool]:
        """(fact text, applied?, superseded?) for one authorization."""
        authority = auth.authority or "an account the explorer does not name"
        target, status = auth.delegate, auth.status
        clears = target == ZERO_ADDRESS
        what = ("clear its code delegation" if clears else
                f"run the code of {target}" if target else "change its code delegation (target not reported)")
        superseded = status == "ok" and not_last_valid
        applied = (not superseded) if status == "ok" else (False if status else None)
        if status == "ok" and target is None:
            text = f"Code delegation of {authority} changed by this transaction (EIP-7702); the explorer does " \
                   "not report the new target."
        elif superseded:
            text = f"EIP-7702 authorization for {authority} to {what} was valid but replaced by a later " \
                   "authorization for the same account in this transaction."
        elif status == "ok" and clears:
            text = f"Code delegation cleared by this transaction: {authority} stops running delegated code (EIP-7702)."
        elif status == "ok":
            text = f"Code delegation set by this transaction: from here on, {authority} runs the code of {target} (EIP-7702)."
        else:
            verdict = (f"was not applied: the explorer marks it {status!r}" if status
                       else "has no validity reported by the explorer")
            text = f"EIP-7702 authorization for {authority} to {what} {verdict}."
        return text, applied, superseded

    def _delegations_applied(self, tx: Transaction) -> dict[str, str]:
        """Lowercase authority -> delegate address in effect after this tx's valid authorizations
        (ZERO_ADDRESS when cleared). Authorizations are applied before execution; the last valid one wins."""
        result: dict[str, str] = {}
        for auth in tx.authorizations:
            if auth.status != "ok" or auth.authority is None:
                continue
            if auth.delegate is None:
                result.pop(auth.authority.lower(), None)  # target unknown: claim nothing
            else:
                result[auth.authority.lower()] = auth.delegate
        return result

    def _add_data_to_codeless(self, to_text: str, size: int, cleared_here: bool, today_basis: str,
                              source: Source) -> None:
        """Data sent to an account without code today. We only assert "no code" when this very
        transaction cleared the account's delegation; otherwise we say what is unknown.

        Why not ask the node for the code before the block: code can appear earlier in the
        same block, and precompiles (which differ per chain and fork) run with no stored code.
        """
        if cleared_here:
            self.bundle.add("call", f"Sent {size} bytes of data to {to_text}, whose code delegation this transaction "
                            "cleared before running; data sent to an account without code is not a function call.",
                            [source], {"data_bytes": size, "had_code": False})
            return
        self.bundle.add("call", f"Sent {size} bytes of data to {to_text}. {today_basis}; this tool cannot confirm "
                        "the account's code at the moment the transaction ran, so it does not say whether this "
                        "was a function call.", [source], {"data_bytes": size, "had_code": None})
        self._gap("Code at execution time", "an account's code during a transaction cannot be confirmed from the "
                  "explorer or a plain state read (code can change within a block, and built-in precompiles run "
                  "without stored code)", "An execution trace of the transaction (debug/trace RPC)", retryable=False, cause="not_interpretable")

    def _add_call(self, tx_hash: str, tx: Transaction) -> None:
        b, sym = self.bundle, self.cfg.network.native_symbol
        source = self._tx_source(tx_hash)
        data, to = tx.raw_input, tx.to
        if data is None:
            self._gap("Call decoding", "the explorer's call data is not readable", "Check the input on the explorer page",
                      retryable=False, cause="source_error")
            return
        if to is not None and to.address is None:
            self._gap("Call decoding", "the explorer did not report the target address of the call",
                      "Check the transaction on the explorer page", retryable=False, cause="source_error")
            return
        if to is None:
            created = tx.created_contract
            text = f"Contract creation: deployed {self._party(created)}." if created else "Contract creation transaction."
            b.add("call", text, [source], {"created": address_of(created)})
            return
        if address_of(tx.sender) == ZERO_ADDRESS:
            text = f"System transaction: sent from the zero address, which no one can sign for, to {self._party(to)}."
            if data == "0x":
                text += " It carries no call data."
            b.add("call", text, [source], {"system": True})
            if data == "0x":
                return
        if data == "0x":
            if tx.authorizations and not tx.value:
                b.add("call", f"No call data and no value were sent; the transaction carries {len(tx.authorizations)} "
                      "EIP-7702 authorization(s), listed as delegation facts.", [source])
            else:
                b.add("call", f"Plain {sym} transfer (no call data) to {self._party(to)}.", [source])
            return
        if self._is_native_contract(to.address):
            self._add_native_contract_call(self._party(to), to.address, data, source)
            return
        delegate_here = self._delegations_applied(tx).get(to.address.lower())
        if delegate_here == ZERO_ADDRESS or (
                delegate_here is None and to.is_contract is False and not to.implementations):
            self._add_data_to_codeless(self._party(to), (len(data) - 2) // 2, delegate_here == ZERO_ADDRESS,
                                       "The explorer lists it as having no contract code today", source)
            return

        # Code the account ran: a delegation set by this very tx first, then today's listed implementations.
        decoder = self._decoder_for(to.address, ([delegate_here] if delegate_here else []) + list(to.implementations))
        decoded = decoder.decode_call(data) if decoder else None
        if decoded is None:
            b.add("call", f"Called function with selector {data[:10]} on {self._party(to)}; not decoded.",
                  [source], {"selector": data[:10]})
            self._declare_undecoded("Call decoding", to.address, f"selector {data[:10]}", code_owner=delegate_here)
            return
        abi_source = self._api_source(f"/smart-contracts/{to.address}", "Explorer API: contract ABI")
        b.add("call",
              f"Called {decoded.signature} on {self._party(to)}{with_args(decoded.args)}. "
              f"ABI source: {self.abi_notes[to.address.lower()]}.",
              [source, abi_source],
              {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}})

    def _add_native_contract_call(self, to_text: str, address: str, data: str, source: Source) -> None:
        """A call to a contract built into the node: it runs without bytecode, but it can have a
        published ABI (e.g. Rootstock's Bridge), so the normal ABI lookup still applies."""
        decoder = self._decoder_for(address) if self.explorer_answered else None
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            self.bundle.add("call", f"Called {decoded.signature} on {to_text}{with_args(decoded.args)}. "
                            f"ABI source: {self.abi_notes[address.lower()]}.", [source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args},
                             "native_contract": True})
            return
        self.bundle.add("call", f"Called {to_text}, a contract built into the network's node, with selector "
                        f"{data[:10]}; not decoded.", [source], {"selector": data[:10], "native_contract": True})
        self._declare_undecoded("Call decoding", address, f"selector {data[:10]}")

    def _decoder_for(self, address: str, implementations: list[str] | None = None) -> AbiDecoder | None:
        """ABI decoder for a contract, cached per run. Records where the ABI came from."""
        key = address.lower()
        if key in self.decoders:
            return self.decoders[key]
        decoder, note = None, "none (explorer ABI disabled in config)"
        if "explorer" in self.cfg.abi_strategy.order:
            lookup = self.explorer.abi_for(address, implementations)
            decoder = AbiDecoder(lookup.abi) if lookup.abi else None
            note = f"explorer: {lookup.note}" if decoder else "none"
            if lookup.failures:
                retryable = any(f.retryable for f in lookup.failures)
                self.abi_lookup_failed[key] = retryable
                self._gap("ABI lookup", f"the ABI lookup for {address} failed: {lookup.failures[0]}",
                          "Try again in a few minutes" if retryable else "Check the contract on the explorer page",
                          retryable, _source_cause(retryable))
        self.decoders[key] = decoder
        self.abi_notes[key] = note
        self.bundle.abi_sources[address] = note
        return decoder

    def _declare_undecoded(self, topic: str, address: str, what: str, code_owner: str | None = None) -> None:
        """Gap for something we could not decode, saying whether the ABI is missing or just unreachable."""
        if address.lower() in self.abi_lookup_failed:
            retryable = self.abi_lookup_failed[address.lower()]
            self._gap(topic, f"{what} on {address} not decoded because the ABI lookup failed",
                      "Try again in a few minutes" if retryable else "Check the contract on the explorer page",
                      retryable, _source_cause(retryable))
        elif self._is_native_contract(address):
            self._gap(topic, f"{address} is a native contract built into the node and the explorer has no verified "
                      f"ABI for it, so {what} cannot be decoded", "Its published ABI (e.g. from the network node's "
                      "source) in a configured repo", retryable=False, cause="not_interpretable")
        elif code_owner:
            self._gap(topic, f"{address} ran the code of {code_owner} (EIP-7702), and no ABI for it matches {what}",
                      f"{code_owner} verified on the explorer, or its ABI in a configured repo", retryable=False,
                      cause="not_interpretable")
        else:
            self._gap(topic, f"no ABI for {address} matches {what}",
                      "A verified contract on the explorer, or the contract ABI in a configured repo",
                      retryable=False, cause="not_interpretable")

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
        for it in calls[:MAX_INTERNAL]:
            text, moves_value = self._internal_text(it)
            same_as = self._same_native_movement(it)
            if same_as:
                text = text.rstrip(".") + f": the same movement as {same_as}, not an additional one."
            self.bundle.add("internal_call", text, [source],
                            {"type": it.type, "value": str(it.value), "moves_value": moves_value, "same_as": same_as})
        if reads:
            self.bundle.add("internal_call", f"{len(reads)} read-only staticcall(s) (no state change) not listed.",
                            [source], {"staticcalls": len(reads)})
        if system:
            moved = sum(it.value for it in system)
            text = f"{len(system)} internal call(s) between the network's system contracts not listed"
            text += f" (they carry {self._native_amount(moved)} in total)." if moved else " (no value carried)."
            self.bundle.add("internal_call", text, [source], {"system_calls": len(system), "value": str(moved)})
        if truncated or len(calls) > MAX_INTERNAL:
            self._gap("Internal calls", f"showing the first {MAX_INTERNAL}",
                      "Open the explorer page for the full list", retryable=False, cause="not_interpretable")

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
                return f"Internal {kind} by {creator} failed: nothing was deployed or transferred.", False
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
            return f"Internal call {route} tried to send {shown} but failed; nothing was transferred.", False

        text = f"Internal {kind} {route}" + ("" if ok else " (this internal call failed)")
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
            self.bundle.add("event", f"Event {event.signature} emitted by {self._party(emitter)}{args}.", [source],
                            {"event": event.name, "args": {a.name: a.value for a in event.args}})
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

    def _declare_undecoded_events(self) -> None:
        """One gap per contract with undecoded events, not one per event."""
        for address in sorted(self.undecoded_events):
            self._declare_undecoded("Event decoding", address, "its events")
        for address in sorted(self.anonymous_unmatched):
            self._gap("Event decoding", f"{address} declares anonymous events (no signature topic); some of its "
                      "logs fit none or several of them", "Check those logs on the explorer page", retryable=False, cause="not_interpretable")

    # ---- rpc path ----------------------------------------------------------

    def _add_rpc_only(self, tx_hash: str, view: RpcView) -> None:
        b, tx, receipt = self.bundle, view.tx, view.receipt
        source = self._rpc_source(f"eth_getTransactionByHash + eth_getTransactionReceipt [{tx_hash}]")
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        value = amount(tx.value or 0, dec)

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
        decoder = self._decoder_for(to) if self.explorer_answered else None
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            self.bundle.add("call", f"Called {decoded.signature} on {to}{with_args(decoded.args)}. "
                            f"ABI source: {self.abi_notes[to.lower()]}.", [source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}})
            return
        self.bundle.add("call", f"Called function with selector {data[:10]} on {to}; not decoded.", [source],
                        {"selector": data[:10]})
        if self.explorer_answered:
            self._declare_undecoded("Call decoding", to, f"selector {data[:10]}")
        else:
            self._gap("Call decoding", "explorer unavailable, so no ABI could be fetched",
                      "Explorer API reachable, or the contract ABI in a configured repo", retryable=True,
                      cause="source_unavailable")

    def _add_cross_check(self, view: RpcView) -> None:
        receipt = view.receipt
        if receipt is None or self.bundle.status not in ("success", "failed"):
            return
        if receipt.status == "unknown":
            return  # receipt without a status field: nothing to compare
        agree = receipt.status == self.bundle.status
        text = f"RPC receipt independently reports status {receipt.status}"
        text += ", matching the explorer." if agree else f", but the explorer says {self.bundle.status}."
        source = self._rpc_source(f"eth_getTransactionReceipt [{self.bundle.tx_hash}]")
        self.bundle.add("cross_check", text, [source], {"rpc_status": receipt.status, "agrees": agree})
        if not agree:
            self._gap("Status disagreement", "explorer and RPC report different statuses",
                      "Treat the RPC receipt as authoritative and re-check the explorer index", retryable=True,
                      cause="source_behind")


class UnreadableLog(ValueError):
    """The explorer sent a log this tool cannot read (a source problem, not a defect here)."""


def _source_cause(retryable: bool) -> GapCause:
    """A source that failed: unavailable (worth retrying) or refused the request."""
    return "source_unavailable" if retryable else "source_error"


def build_bundle(tx_hash: str, cfg: AppConfig, explorer: ExplorerClient | None = None,
                 rpc: RpcClient | None = None) -> EvidenceBundle:
    """Collect everything about one transaction into a numbered, sourced bundle."""
    if not HASH_RE.match(tx_hash):
        raise InvalidHashError("Not a valid transaction hash (expected 0x followed by 64 hex characters).")
    budget = Budget(cfg.assistant.time_budget_s)
    explorer = explorer or ExplorerClient(cfg.explorer, budget=budget)
    rpc = rpc or RpcClient(cfg.rpc, budget=budget)
    return BundleBuilder(cfg, explorer, rpc).build(tx_hash)
