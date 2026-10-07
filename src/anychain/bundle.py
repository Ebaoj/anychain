"""Build the evidence bundle: deterministic, no LLM. Every fact gets an id and a source.

Reading order: `build()` runs the steps; each `_add_*` method adds facts for one topic.
Each step is wrapped by `_safely()`, so an unexpected payload costs one topic
(declared as a gap), never the whole answer.
"""
import re
from collections.abc import Callable

from eth_utils import to_checksum_address

from anychain.collectors.explorer import MAX_PAGES, ExplorerClient
from anychain.collectors.http import Budget, CollectorError, NotFoundError
from anychain.collectors.rpc import RpcClient
from anychain.config import AppConfig
from anychain.decoder import AbiDecoder
from anychain.chains import BLOCKSCOUT_CHAIN_TYPES, Fee, audit_fields, profile_for
from anychain.models import EvidenceBundle, Source

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_EVENTS = 40
MAX_INTERNAL = 30
ZERO_ADDRESS = "0x" + "0" * 40

# Internal call types whose `value` is only the caller's context, never a payment:
# delegatecall and callcode run another contract's code inside the caller, so any
# value stays with the caller; staticcall cannot carry value. (See DECISIONS D17.)
VALUE_IS_CONTEXT = {"delegatecall", "callcode", "staticcall"}

class InvalidHashError(ValueError):
    """The input is not a transaction hash."""


# Blockscout `status` -> our status. `status` is null while pending.
EXPLORER_STATUS = {"ok": "success", "error": "failed"}
# Blockscout `result` values that describe state, not a failure reason.
RESULT_WITHOUT_REASON = {
    "success", "pending", "awaiting_internal_transactions", "dropped/replaced", "Reverted", "error",
}


# ---- small pure helpers ------------------------------------------------------

def to_int(value: object) -> int | None:
    """Parse an int from a decimal string, a 0x-hex string or an int. None if impossible."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def amount(raw: int, decimals: int) -> str:
    """Exact decimal string for an integer amount (no float rounding)."""
    if decimals <= 0:
        return str(raw)
    whole, frac = divmod(raw, 10**decimals)
    if frac == 0:
        return str(whole)
    return f"{whole}.{str(frac).rjust(decimals, '0').rstrip('0')}"


def party(value: dict | str | None, labels: dict[str, str] | None = None) -> str:
    """An address with its explorer name, or else the config's label for it (address_labels)."""
    if not value:
        return "(none)"
    address = value if isinstance(value, str) else value.get("hash", "(unknown address)")
    name = None if isinstance(value, str) else (value.get("name") or value.get("ens_domain_name"))
    name = name or (labels or {}).get(str(address).lower())
    return f"{address} ({name})" if name else address


def implementation_addresses(value: dict) -> list[str]:
    """Proxy implementations the explorer lists next to an address."""
    impls = value.get("implementations") or []
    return [i["address_hash"] for i in impls if isinstance(i, dict) and isinstance(i.get("address_hash"), str)]


def delegate_of(auth: dict) -> str | None:
    """Delegate address of an EIP-7702 authorization. Blockscout calls it `address_hash`
    (older versions: `address`). None when absent: never read a missing value as "cleared"."""
    target = auth.get("address_hash") or auth.get("address")
    return target if isinstance(target, str) else None


def address_of(value: dict | None) -> str | None:
    return value.get("hash") if isinstance(value, dict) else None


def receipt_status(receipt: dict) -> str:
    """'success' / 'failed' from a receipt, or 'unknown' for pre-Byzantium receipts (no status field)."""
    return {"0x1": "success", "0x0": "failed"}.get(receipt.get("status"), "unknown")


def describe_revert(reason: object) -> str:
    """Blockscout gives the revert reason decoded (dict), raw ({'raw': '0x..'}) or as text."""
    if isinstance(reason, dict):
        if reason.get("method_call"):
            params = ", ".join(f"{p.get('name')}={p.get('value')!r}" for p in reason.get("parameters") or [])
            return f"{reason['method_call']} with {params}" if params else reason["method_call"]
        if reason.get("raw"):
            return f"undecoded revert data {reason['raw']}"
    return repr(reason)


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
        self.undecoded_events: set[str] = set()
        self.anonymous_unmatched: set[str] = set()
        self.bundle = EvidenceBundle(network=cfg.network.name, tx_hash="", status="unknown")
        self.profile, self.dedicated_profile = profile_for(cfg.network.chain_type)

    def build(self, tx_hash: str) -> EvidenceBundle:
        self.bundle = EvidenceBundle(network=self.cfg.network.name, tx_hash=tx_hash, status="unknown")
        tx = self._fetch_explorer_tx(tx_hash)
        explorer_status = self._status_from_explorer(tx) if tx is not None else None
        rpc_view = self._fetch_rpc(tx_hash, explorer_status)

        has_receipt = rpc_view is not None and bool(rpc_view["receipt"])
        explorer_lagging = explorer_status in ("pending", "dropped") and has_receipt
        if explorer_lagging:
            self._gap("Explorer index", f"the explorer shows this transaction as {explorer_status}, "
                      "but the RPC node already has its receipt", "Ask again in a minute for full details",
                      retryable=True)
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
                      "Ask again in a minute for decoded details", retryable=True)
        else:
            self._gap("Explorer transaction data", "the explorer does not know this hash",
                      "Check the hash and that the config points to the right network", retryable=False)

    # ---- plumbing ----------------------------------------------------------

    def _safely(self, topic: str, step: Callable[[], None]) -> None:
        """Run one step; an unexpected error becomes a gap instead of a crash."""
        try:
            step()
        except CollectorError as exc:
            needed = "Try again in a few minutes" if exc.retryable else "Check this part on the explorer page"
            self._gap(topic, str(exc), needed, exc.retryable)
        except Exception as exc:  # unexpected payload shape, decoding edge case...
            self._gap(topic, f"could not process the data ({type(exc).__name__}: {exc})",
                      "Check this part on the explorer page", retryable=False)

    def _party(self, value: dict | str | None) -> str:
        return party(value, self.cfg.address_labels)

    def _gap(self, what: str, why: str, needed: str, retryable: bool) -> None:
        self.bundle.add_gap(what, why, needed, retryable)

    def _api_source(self, path: str, label: str) -> Source:
        return Source(kind="explorer_api", label=label, url=self.explorer.url(path))

    def _rpc_source(self, detail: str) -> Source:
        return Source(kind="rpc", label="JSON-RPC", url=self.cfg.rpc.url, detail=detail)

    # ---- fetching ----------------------------------------------------------

    def _fetch_explorer_tx(self, tx_hash: str) -> dict | None:
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
                      "Explorer API reachable and indexing this transaction", exc.retryable)
            return None

    def _fetch_rpc(self, tx_hash: str, explorer_status: str | None) -> dict | None:
        """Transaction + receipt from the RPC node, after checking it is on the right chain."""
        try:
            chain_id = self.rpc.chain_id()
            if chain_id != self.cfg.network.chain_id:
                self._gap("RPC transaction data",
                          f"the RPC is on chain {chain_id}, but the config says "
                          f"{self.cfg.network.chain_id}; RPC data ignored",
                          "Fix rpc.url or network.chain_id in the config", retryable=False)
                return None
            tx = self.rpc.transaction(tx_hash)
            receipt = self.rpc.receipt(tx_hash) if tx else None
        except CollectorError as exc:
            self._gap("RPC transaction data", str(exc), "RPC endpoint reachable", exc.retryable)
            return None
        except Exception as exc:  # unexpected payload shape
            self._gap("RPC transaction data", f"could not process the RPC answer ({type(exc).__name__}: {exc})",
                      "Check the RPC endpoint", retryable=False)
            return None
        if tx is None:
            self._explain_rpc_miss(explorer_status)
            return None
        return {"tx": tx, "receipt": receipt}

    def _explain_rpc_miss(self, explorer_status: str | None) -> None:
        """The node returned null. Why depends on what the explorer knows."""
        if explorer_status is None:
            self._gap("RPC transaction data", "the RPC node does not know this hash",
                      "Check the hash and that the config points to the right network", retryable=False)
        elif explorer_status in ("pending", "dropped"):
            return  # not mined: the node not having it is expected
        else:
            self._gap("RPC transaction data",
                      "the RPC node returned nothing for a transaction the explorer has; it may not keep old history",
                      "An RPC endpoint with full (archive) history", retryable=False)

    # ---- explorer path -----------------------------------------------------

    def _describe_from_explorer(self, tx_hash: str, tx: dict, status: str) -> None:
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
        self._safely("Token transfers", lambda: covered_logs.update(self._add_transfers(tx_hash)))
        self._safely("Internal calls", lambda: self._add_internal(tx_hash, tx))
        self._safely("Events", lambda: self._add_events(tx_hash, covered_logs))
        self._declare_undecoded_events()

    def _status_from_explorer(self, tx: dict) -> str:
        # Blockscout stores dropped transactions with status "error", so check `result` first.
        if tx.get("result") == "dropped/replaced":
            return "dropped"
        raw_status = tx.get("status")
        if isinstance(raw_status, str) and raw_status in EXPLORER_STATUS:
            return EXPLORER_STATUS[raw_status]
        if tx.get("result") == "pending":
            return "pending"
        return "unknown"

    def _add_overview(self, tx_hash: str, tx: dict) -> None:
        b, cfg = self.bundle, self.cfg
        sources = [Source(kind="explorer_ui", label="Explorer page", url=cfg.explorer.tx_url(tx_hash)),
                   self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")]
        sym = cfg.network.native_symbol
        value_raw = to_int(tx.get("value"))
        value = amount(value_raw, cfg.network.native_decimals) if value_raw is not None else "unknown"
        sender, target = self._party(tx.get("from")), self._target_text(tx)
        data = {"status": b.status, "from": address_of(tx.get("from")), "to": address_of(tx.get("to")),
                "value": value, "tx_type": tx.get("type")}

        if b.status == "pending":
            b.add("overview", f"Transaction is pending: not yet included in a block. From {sender} to {target}. "
                  f"Native value offered: {value} {sym}. Nothing is final until it is mined.", sources, data)
            self._gap("Final outcome", "the transaction is still pending",
                      "Wait for it to be mined, then ask again", retryable=True)
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
        data |= {"block": tx.get("block_number"), "timestamp": tx.get("timestamp"),
                 "gas_used": tx.get("gas_used"), "gas_limit": tx.get("gas_limit")}
        b.add("overview",
              f"Transaction {verb} in block {tx.get('block_number')} at {tx.get('timestamp')}. "
              f"From {sender} to {target}. {value_text} "
              f"Gas used {tx.get('gas_used')} of limit {tx.get('gas_limit')}.",
              sources, data)

    def _target_text(self, tx: dict) -> str:
        if tx.get("to"):
            return self._party(tx["to"])
        created = tx.get("created_contract")
        return f"(contract creation of {self._party(created)})" if created else "(contract creation)"

    def _add_fee(self, tx_hash: str, tx: dict) -> None:
        fee = self.profile.fee(tx)
        if fee is None:
            return
        text = f"Fee paid: {self._fee_text(fee)}."
        for warning in fee.warnings:
            self._gap("Fee", warning, "Check the fee on the explorer page", retryable=False)
        unknown_decimals = [p.token for p in fee.parts if p.token and p.token.decimals is None]
        for token in unknown_decimals:
            self._gap("Fee", f"the explorer does not report the decimals of the fee token {token.address}, so the "
                      "fee is shown in raw units", "The token's decimals (its contract or the explorer's token page)",
                      retryable=False)
        if self.bundle.status == "failed":
            text += " The fee is charged even though the transaction failed."
        source = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        self.bundle.add("fee", text, [source], {
            "fee": self._fee_amount(fee.total_raw, fee.single_token),
            "token": fee.single_token.symbol if fee.single_token else self.cfg.network.native_symbol,
            "parts": {p.label: self._fee_amount(p.raw, p.token) for p in fee.parts},
        })

    def _fee_amount(self, raw: int, token) -> str:
        """Decimal amount; raw integer units when the token's decimals are unknown."""
        if token is not None and token.decimals is None:
            return str(raw)
        return amount(raw, token.decimals if token else self.cfg.network.native_decimals)

    def _fee_text(self, fee: Fee) -> str:
        """'0.0001 ETH', '0.0001 ETH in total: 0.00008 ETH L2 execution + 0.00002 ETH L1 data', or a token fee."""
        def money(raw, token):
            if token is not None and token.decimals is None:
                return f"{raw} raw units of {token.symbol}"
            return f"{self._fee_amount(raw, token)} {token.symbol if token else self.cfg.network.native_symbol}"
        token = fee.single_token
        if len(fee.parts) == 1:
            text = money(fee.total_raw, token)
        elif token is not None or all(p.token is None for p in fee.parts):
            parts = " + ".join(f"{money(p.raw, p.token)} {p.label}" for p in fee.parts)
            text = f"{money(fee.total_raw, token)} in total: {parts}"
        else:
            text = " + ".join(f"{money(p.raw, p.token)} {p.label}" for p in fee.parts)
        return f"{text}; {fee.note}" if fee.note else text

    def _add_chain_facts(self, tx_hash: str, tx: dict) -> None:
        """Facts only this network type has, plus a check that the config's chain_type fits the payload."""
        source = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        for fact in self.profile.facts(tx):
            self.bundle.add(fact.kind, fact.text, [source], fact.data)
        configured = self.cfg.network.chain_type
        if configured not in BLOCKSCOUT_CHAIN_TYPES:
            self._gap("Chain type", f"chain_type {configured!r} is not a Blockscout CHAIN_TYPE value this tool knows "
                      f"(typo, or an older/newer Blockscout); the generic profile is used",
                      f"Check network.chain_type; known values: {', '.join(sorted(BLOCKSCOUT_CHAIN_TYPES))}",
                      retryable=False)
        elif not self.dedicated_profile:
            self._gap("Network-specific details", f"chain_type {configured!r} has no dedicated profile yet, so only "
                      "the details common to every EVM network are interpreted",
                      "A profile for this chain type in chains.py", retryable=False)
        audit = audit_fields(tx, self.profile)
        for other_type, names in audit.other_types.items():
            self._gap("Chain type", f"the config says chain_type {configured!r}, but the explorer reports "
                      f"fields typical of {other_type!r} ({', '.join(names)})",
                      f"Check network.chain_type; if this network is {other_type!r}, set it so these are interpreted",
                      retryable=False)
        if audit.unknown:
            self._gap("Network-specific details", f"the explorer reports data this tool does not interpret: "
                      f"{', '.join(audit.unknown)}", "A chain type profile that covers these fields",
                      retryable=False)

    def _add_revert(self, tx_hash: str, tx: dict) -> None:
        if self.bundle.status != "failed":
            return
        source = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        reason = tx.get("revert_reason")
        result = tx.get("result")
        if isinstance(reason, dict) and not reason.get("method_call") and reason.get("raw") in ("0x", ""):
            self.bundle.add("revert", "The transaction reverted without any revert data: no reason text and no "
                            "custom error (e.g. a bare revert(), a failed assert in old Solidity, or a call to "
                            "code that does not exist).", [source], {"revert_reason": reason})
            return
        if isinstance(reason, dict) and not reason.get("method_call") and not reason.get("raw"):
            reason = None  # e.g. {"raw": null}: nothing reported
        if reason:
            self.bundle.add("revert", f"Explorer reports the revert reason: {describe_revert(reason)}.",
                            [source], {"revert_reason": reason})
        elif isinstance(result, str) and result not in RESULT_WITHOUT_REASON:
            self.bundle.add("revert", f"Explorer reports the failure as: {result!r}.", [source], {"result": result})
        else:
            self._gap("Revert reason", "the explorer did not report why the transaction failed",
                      "A node that can re-execute or trace the call (debug/trace RPC)", retryable=False)

    def _add_authorizations(self, tx_hash: str, tx: dict) -> None:
        """EIP-7702 (type 4): accounts that set or cleared the contract code they run.

        Only an authorization the explorer marks "ok" took effect, and when one account
        has several valid ones in the same tx, the last one wins (EIP-7702 order).
        """
        source = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        auths = [a for a in tx.get("authorization_list") or [] if isinstance(a, dict)]
        last_valid = {str(a.get("authority")).lower(): n for n, a in enumerate(auths) if a.get("status") == "ok"}
        for n, auth in enumerate(auths):
            authority = auth.get("authority") or "an account the explorer does not name"
            target, status = delegate_of(auth), auth.get("status")
            clears = target == ZERO_ADDRESS
            what = ("clear its code delegation" if clears else
                    f"run the code of {target}" if target else "change its code delegation (target not reported)")
            if status == "ok" and target is None:
                text = f"Code delegation of {authority} changed by this transaction (EIP-7702); the explorer does " \
                       "not report the new target."
            elif status == "ok" and last_valid.get(str(auth.get("authority")).lower()) != n:
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
            superseded = status == "ok" and last_valid.get(str(auth.get("authority")).lower()) != n
            applied = (False if superseded else True) if status == "ok" else (False if status else None)
            self.bundle.add("delegation", text, [source],
                            {"authority": auth.get("authority"), "delegate": target, "status": status,
                             "applied": applied, "superseded": superseded})

    def _delegations_applied(self, tx: dict) -> dict[str, str]:
        """Lowercase authority -> delegate address in effect after this tx's valid authorizations
        (ZERO_ADDRESS when cleared). Authorizations are applied before execution; the last valid one wins."""
        result = {}
        for auth in tx.get("authorization_list") or []:
            if isinstance(auth, dict) and auth.get("status") == "ok" and isinstance(auth.get("authority"), str):
                if delegate_of(auth) is None:
                    result.pop(auth["authority"].lower(), None)  # target unknown: claim nothing
                else:
                    result[auth["authority"].lower()] = delegate_of(auth)
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
                  "without stored code)", "An execution trace of the transaction (debug/trace RPC)", retryable=False)

    def _add_call(self, tx_hash: str, tx: dict) -> None:
        b, sym = self.bundle, self.cfg.network.native_symbol
        source = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        data = tx.get("raw_input") or "0x"
        to = tx.get("to")
        if not to:
            created = tx.get("created_contract")
            text = f"Contract creation: deployed {self._party(created)}." if created else "Contract creation transaction."
            b.add("call", text, [source], {"created": address_of(created)})
            return
        authorizations = tx.get("authorization_list") or []
        if address_of(tx.get("from")) == ZERO_ADDRESS:
            text = f"System transaction: sent from the zero address, which no one can sign for, to {self._party(to)}."
            if data == "0x":
                text += " It carries no call data."
            b.add("call", text, [source], {"system": True})
            if data == "0x":
                return
        if data == "0x":
            if authorizations and not to_int(tx.get("value")):
                b.add("call", f"No call data and no value were sent; the transaction carries {len(authorizations)} "
                      "EIP-7702 authorization(s), listed as delegation facts.", [source])
            else:
                b.add("call", f"Plain {sym} transfer (no call data) to {self._party(to)}.", [source])
            return
        delegations = self._delegations_applied(tx)
        delegate_here = delegations.get(to["hash"].lower())
        if delegate_here == ZERO_ADDRESS or (
                delegate_here is None and to.get("is_contract") is False and not implementation_addresses(to)):
            self._add_data_to_codeless(self._party(to), (len(data) - 2) // 2, delegate_here == ZERO_ADDRESS,
                                       "The explorer lists it as having no contract code today", source)
            return

        address = to["hash"]
        # Code the account ran: a delegation set by this very tx first, then today's listed implementations.
        decoder = self._decoder_for(address, ([delegate_here] if delegate_here else []) + implementation_addresses(to))
        decoded = decoder.decode_call(data) if decoder else None
        if decoded is None:
            b.add("call", f"Called function with selector {data[:10]} on {self._party(to)}; not decoded.",
                  [source], {"selector": data[:10]})
            self._declare_undecoded("Call decoding", address, f"selector {data[:10]}", code_owner=delegate_here)
            return
        args = ", ".join(f"{a.name}={a.value}" for a in decoded.args)
        abi_source = self._api_source(f"/smart-contracts/{address}", "Explorer API: contract ABI")
        b.add("call",
              f"Called {decoded.signature} on {self._party(to)} with {args}. "
              f"ABI source: {self.abi_notes[address.lower()]}.",
              [source, abi_source],
              {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}})

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
                          retryable)
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
                      retryable)
        elif code_owner:
            self._gap(topic, f"{address} ran the code of {code_owner} (EIP-7702), and no ABI for it matches {what}",
                      f"{code_owner} verified on the explorer, or its ABI in a configured repo", retryable=False)
        else:
            self._gap(topic, f"no ABI for {address} matches {what}",
                      "A verified contract on the explorer, or the contract ABI in a configured repo",
                      retryable=False)

    def _add_transfers(self, tx_hash: str) -> set[int]:
        """Token transfers. Returns the log indexes they cover, so events skip them."""
        items, truncated = self.explorer.token_transfers(tx_hash)
        source = self._api_source(f"/transactions/{tx_hash}/token-transfers", "Explorer API: token transfers")
        covered: set[int] = set()
        for t in items:
            if to_int(t.get("log_index")) is not None:
                covered.add(to_int(t.get("log_index")))  # type: ignore[arg-type]
            token = t.get("token") or {}
            text = f"Token transfer: {self._transfer_what(token, t.get('total') or {})} " \
                   f"from {self._party(t.get('from'))} to {self._party(t.get('to'))}."
            self.bundle.add("token_transfer", text, [source],
                            {"token": token.get("address_hash"), "from": address_of(t.get("from")),
                             "to": address_of(t.get("to"))})
        if truncated:
            self._gap("Token transfers", f"more than {MAX_PAGES} pages of transfers",
                      "Open the explorer page for the full list", retryable=False)
        return covered

    def _transfer_what(self, token: dict, total: dict) -> str:
        """'69.3484 USDC', '50 x NAME token #7', 'NAME token #443098', or the raw amount."""
        symbol = token.get("symbol") or token.get("name") or token.get("address_hash") or "unknown token"
        value = to_int(total.get("value"))
        decimals = to_int(total.get("decimals") if total.get("decimals") is not None else token.get("decimals"))
        token_id = total.get("token_id")
        if token_id is not None and value is not None:
            return f"{value} x {symbol} token #{token_id}"  # ERC-1155: an id and a quantity
        if token_id is not None:
            return f"{symbol} token #{token_id}"  # ERC-721: one NFT
        if value is not None and decimals is not None:
            return f"{amount(value, decimals)} {symbol}"
        if value is not None:
            return f"{value} raw units of {symbol} (decimals unknown)"
        return f"{symbol} (amount not reported by the explorer)"

    def _add_internal(self, tx_hash: str, tx: dict) -> None:
        if tx.get("result") == "awaiting_internal_transactions":
            self._gap("Internal calls", "the explorer is still indexing this transaction's internal calls",
                      "Ask again in a few minutes", retryable=True)
            return
        items, truncated = self.explorer.internal_transactions(tx_hash)
        path = f"/transactions/{tx_hash}/internal-transactions"
        source = self._api_source(path, "Explorer API: internal transactions")
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        reads = [it for it in items if it.get("type") == "staticcall"]
        calls = [it for it in items if it.get("type") != "staticcall"]
        for it in calls[:MAX_INTERNAL]:
            text, moves_value = self._internal_text(it, sym, dec)
            self.bundle.add("internal_call", text, [source],
                            {"type": it.get("type"), "value": str(to_int(it.get("value")) or 0),
                             "moves_value": moves_value})
        if reads:
            self.bundle.add("internal_call", f"{len(reads)} read-only staticcall(s) (no state change) not listed.",
                            [source], {"staticcalls": len(reads)})
        if truncated or len(calls) > MAX_INTERNAL:
            self._gap("Internal calls", f"showing the first {MAX_INTERNAL}",
                      "Open the explorer page for the full list", retryable=False)

    def _internal_text(self, it: dict, sym: str, dec: int) -> tuple[str, bool | None]:
        """Describe one internal call, and say whether native value really moved (None: not known)."""
        kind = it.get("type")
        value = to_int(it.get("value")) or 0
        shown = f"{amount(value, dec)} {sym}"
        success = it.get("success")  # True, False, or None when the explorer does not say
        ok = success is not False

        if kind in ("create", "create2"):
            creator = self._party(it.get("from"))
            if success is None:
                return f"Internal {kind} by {creator}; the explorer does not say if the deployment succeeded.", None
            if not ok:
                return f"Internal {kind} by {creator} failed: nothing was deployed or transferred.", False
            created = it.get("created_contract")
            deployed = self._party(created) if created else "a contract whose address the explorer does not report"
            text = f"Internal {kind} by {creator}: deployed {deployed}"
            return (f"{text} with {shown}." if value > 0 else f"{text}."), value > 0

        route = f"from {self._party(it.get('from'))} to {self._party(it.get('to'))}"
        if kind == "call" and value > 0:
            if success is None:
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
        logs = [log for log in items if to_int(log.get("index")) not in covered_logs]
        if len(logs) > MAX_EVENTS:
            logs, truncated = logs[:MAX_EVENTS], True
        broken = 0
        for log in logs:
            try:
                self._add_one_event(log, source)
            except Exception:  # one odd log must not cost the others
                broken += 1
        if broken:
            self._gap("Events", f"{broken} log(s) had an unexpected shape and were skipped",
                      "Check the logs on the explorer page", retryable=False)
        if truncated:
            self._gap("Events", f"showing the first {MAX_EVENTS}", "Open the explorer page for the full list",
                      retryable=False)

    def _add_one_event(self, log: dict, source: Source) -> None:
        emitter = log.get("address") or {}
        address = emitter.get("hash", "(unknown)")
        topics = [t for t in log.get("topics") or [] if t]
        decoder = self._decoder_for(address, implementation_addresses(emitter))
        event = decoder.decode_log(topics, log.get("data") or "0x") if decoder else None
        if event:
            args = ", ".join(f"{a.name}={a.value}" for a in event.args)
            self.bundle.add("event", f"Event {event.signature} emitted by {self._party(emitter)}: {args}.", [source],
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
                      "logs fit none or several of them", "Check those logs on the explorer page", retryable=False)

    # ---- rpc path ----------------------------------------------------------

    def _add_rpc_only(self, tx_hash: str, view: dict) -> None:
        b, tx, receipt = self.bundle, view["tx"], view["receipt"]
        source = self._rpc_source(f"eth_getTransactionByHash + eth_getTransactionReceipt [{tx_hash}]")
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        value = amount(to_int(tx.get("value")) or 0, dec)

        if not receipt:
            b.status = "pending"
            b.add("overview", f"(From RPC only) Transaction is pending. From {tx.get('from')} to "
                  f"{tx.get('to') or '(contract creation)'}. Native value offered: {value} {sym}.", [source])
            self._gap("Final outcome", "the transaction is still pending", "Wait for it to be mined, then ask again",
                      retryable=True)
            return

        b.status = receipt_status(receipt)
        verb = {"success": "succeeded", "failed": "failed (reverted)"}.get(b.status, "was mined (outcome not recorded)")
        target = tx.get("to") or f"(contract creation of {receipt.get('contractAddress')})"
        if b.status == "failed":
            value_text = f"Native value attached: {value} {sym}, not transferred because the transaction reverted."
        else:
            value_text = f"Native value: {value} {sym}."
        if b.status == "unknown":
            self._gap("Outcome", "this receipt has no status field (the node's receipt format predates it)",
                      "The explorer, which infers the outcome from execution traces", retryable=False)
        b.add("overview",
              f"(From RPC only) Transaction {verb} in block {to_int(tx.get('blockNumber'))}. "
              f"From {tx.get('from')} to {target}. {value_text} "
              f"Gas used {to_int(receipt.get('gasUsed'))} of limit {to_int(tx.get('gas'))}.",
              [source], {"status": b.status, "from": tx.get("from"), "to": tx.get("to")})

        data = tx.get("input") or "0x"
        authorizations = tx.get("authorizationList") or []
        if authorizations:
            delegates = ", ".join(sorted({str(a.get("address")) for a in authorizations if isinstance(a, dict)}))
            b.add("delegation", f"Transaction carries {len(authorizations)} EIP-7702 authorization(s) naming "
                  f"{delegates}; who signed them and whether they were valid is not checked without the explorer.",
                  [source], {"applied": None})
            self._gap("Code delegations", "signers and validity of EIP-7702 authorizations need the explorer",
                      "Explorer API reachable", retryable=True)
        if tx.get("to") and data == "0x" and authorizations and not to_int(tx.get("value")):
            b.add("call", f"No call data and no value were sent; the transaction carries {len(authorizations)} "
                  "EIP-7702 authorization(s).", [source])
        elif tx.get("to") and data != "0x":
            self._add_rpc_call(to_checksum_address(tx["to"]), data, source)
        logs = receipt.get("logs") or []
        b.add("receipt", f"Receipt has {len(logs)} log(s).", [source])
        if logs and self.explorer_answered:
            self._gap("Event decoding", "the explorer has not indexed this transaction's events yet",
                      "Ask again in a minute", retryable=True)
        elif logs:
            self._gap("Event decoding", "explorer unavailable, so events stay undecoded", "Explorer API reachable",
                      retryable=True)

    def _add_rpc_call(self, to: str, data: str, source: Source) -> None:
        """Decode the call from RPC data, using the explorer's ABI when the explorer answers."""
        # Without the explorer there is no "is it a contract?" flag: use the node's code today,
        # the same criterion the explorer applies. Code today = a contract.
        try:
            has_code_today = self.rpc.code_at(to, "latest") not in ("0x", "")
        except CollectorError as exc:
            has_code_today = None
            self._gap("Contract check", f"could not read whether {to} has contract code: {exc}",
                      "Try again in a few minutes", exc.retryable)
        if has_code_today is False:
            self._add_data_to_codeless(to, (len(data) - 2) // 2, False,
                                       "The node shows no contract code at this address today", source)
            return
        decoder = self._decoder_for(to) if self.explorer_answered else None
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            args = ", ".join(f"{a.name}={a.value}" for a in decoded.args)
            self.bundle.add("call", f"Called {decoded.signature} on {to} with {args}. "
                            f"ABI source: {self.abi_notes[to.lower()]}.", [source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}})
            return
        self.bundle.add("call", f"Called function with selector {data[:10]} on {to}; not decoded.", [source],
                        {"selector": data[:10]})
        if self.explorer_answered:
            self._declare_undecoded("Call decoding", to, f"selector {data[:10]}")
        else:
            self._gap("Call decoding", "explorer unavailable, so no ABI could be fetched",
                      "Explorer API reachable, or the contract ABI in a configured repo", retryable=True)

    def _add_cross_check(self, view: dict) -> None:
        receipt = view["receipt"]
        if not receipt or self.bundle.status not in ("success", "failed"):
            return
        rpc_status = receipt_status(receipt)
        if rpc_status == "unknown":
            return  # pre-Byzantium receipt: nothing to compare
        agree = rpc_status == self.bundle.status
        text = f"RPC receipt independently reports status {rpc_status}"
        text += ", matching the explorer." if agree else f", but the explorer says {self.bundle.status}."
        source = self._rpc_source(f"eth_getTransactionReceipt [{self.bundle.tx_hash}]")
        self.bundle.add("cross_check", text, [source], {"rpc_status": rpc_status, "agrees": agree})
        if not agree:
            self._gap("Status disagreement", "explorer and RPC report different statuses",
                      "Treat the RPC receipt as authoritative and re-check the explorer index", retryable=True)


def build_bundle(tx_hash: str, cfg: AppConfig, explorer: ExplorerClient | None = None,
                 rpc: RpcClient | None = None) -> EvidenceBundle:
    """Collect everything about one transaction into a numbered, sourced bundle."""
    if not HASH_RE.match(tx_hash):
        raise InvalidHashError("Not a valid transaction hash (expected 0x followed by 64 hex characters).")
    budget = Budget(cfg.assistant.time_budget_s)
    explorer = explorer or ExplorerClient(cfg.explorer, budget=budget)
    rpc = rpc or RpcClient(cfg.rpc, budget=budget)
    return BundleBuilder(cfg, explorer, rpc).build(tx_hash)
