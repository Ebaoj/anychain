"""Build the evidence bundle: deterministic, no LLM. Every fact gets an id and a source.

Reading order: `build()` runs the steps; each `_add_*` method adds facts for one topic.
Each step is wrapped by `_safely()`, so an unexpected payload costs one topic
(declared as a gap), never the whole answer. Explorer and RPC answers arrive as typed
objects (collectors/types.py); network-type specifics come from a profile (chains.py).
"""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from anychain.chains import profile_for
from anychain.collectors.explorer import ExplorerClient
from anychain.collectors.http import Budget, CollectorError, NotFoundError
from anychain.collectors.repo import Repo
from anychain.collectors.rpc import RpcClient
from anychain.collectors.signatures import SignatureDb
from anychain.collectors.types import AddressRef, Transaction
from anychain.config import AppConfig
from anychain.decoder import AbiDecoder
from anychain.models import EvidenceBundle, GapCause, Source
from anychain.facts.common import (EXPLORER_STATUS, HASH_RE, InvalidHashError, MAX_CODE_LINES, MAX_EVENTS,
    PREFETCH_WORKERS, RpcView, ZERO_ADDRESS, _failure_note, _source_cause, address_of, amount, party, render_code)  # noqa: F401 (some re-exported)
from anychain.facts.calls import CallFacts
from anychain.facts.code import CodeFacts
from anychain.facts.failure import FailureFacts
from anychain.facts.movements import MovementFacts
from anychain.facts.fees import FeeFacts
from anychain.facts.rpc_only import RpcOnlyFacts


class BundleBuilder(CallFacts, CodeFacts, FailureFacts, MovementFacts, FeeFacts, RpcOnlyFacts):
    """Builds the evidence for one transaction. The facts of each concern are in anychain/facts/ (D79);
    this class orchestrates: what is fetched, in which order, and the overview and cross-checks."""

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
        self.rpc_missed = False  # the node answered null for the hash
        self.rpc_view: RpcView | None = None
        self.called_code = None  # (verified index, found function, contract name, address) for the gas notes
        self.repos: list[Repo] | None = None  # configured repos from the local cache, loaded on first use
        self.source_repo: Repo | None = None  # the repo matched to the called contract, if any
        self.abi_origin: dict[str, tuple] = {}  # address -> (repo_pinned | repo_artifact | repo_match, repo, contract or
        #                                         artifact path) when not the explorer
        self.repo_notes: dict[str, str] = {}  # address -> how the repo ABI was chosen
        self.event_topics_asked: set[str] = set()
        self.signatures = (SignatureDb(cfg.abi_strategy.signature_db, cfg.storage.cache_dir, explorer.client,
                                       explorer.budget, cfg.explorer.timeout_s)
                           if "signature_db" in cfg.abi_strategy.order else None)
        self.verified_cache: dict[str, tuple] = {}
        self.undecoded_events: set[str] = set()
        self.anonymous_unmatched: set[str] = set()
        # (from, to, value) of native movements already stated -> the fact id that states them,
        # so an internal call carrying the same value points to it instead of counting it again
        self.native_movement_facts: dict[tuple[str, str, int], str] = {}
        self.bundle = EvidenceBundle(network=cfg.network.name, tx_hash="", status="unknown")
        self.profile, self.dedicated_profile = profile_for(cfg.network.chain_type)

    def build(self, tx_hash: str) -> EvidenceBundle:
        self.bundle = EvidenceBundle(network=self.cfg.network.name, tx_hash=tx_hash, status="unknown")
        # Explorer and node in parallel: a slow explorer must not use up the time the node needs (acceptance
        # run, 2026-10-08: two answers were "unknown" because the node was never asked).
        with ThreadPoolExecutor(max_workers=2) as pool:
            explorer_future = pool.submit(self._fetch_explorer_tx, tx_hash)
            rpc_future = pool.submit(self._fetch_rpc, tx_hash)
            tx, rpc_view = explorer_future.result(), rpc_future.result()
        # same order whichever finished first: the explorer's gaps, then the node's
        self.bundle.gaps.sort(key=lambda g: g.what == "RPC transaction data")
        self.rpc_view = rpc_view
        explorer_status = self._status_from_explorer(tx) if tx is not None else None
        if self.rpc_missed:
            self._explain_rpc_miss(explorer_status)  # why the node has no copy depends on the explorer

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
        if tx is not None and self.bundle.status == "failed" and not explorer_lagging:
            # last, so the facts before it keep their numbers (PHASE4 T2)
            self._safely("Timeline", lambda: self._add_timeline(tx_hash, tx))
        if tx is not None and self.bundle.status in ("success", "failed"):
            self._safely("Gas notes", lambda: self._add_gas_notes(tx))  # last too: earlier numbers stay
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

    def _fetch_rpc(self, tx_hash: str) -> RpcView | None:
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
            self.rpc_missed = True  # explained once the explorer's answer is in (_explain_rpc_miss)
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

    def _prefetch(self, tx_hash: str, tx: Transaction) -> None:
        """Fetch in parallel what the steps below will ask for, one by one, so the answer is the same
        but the explorer's latency is paid once per wave instead of once per request (D26).

        Wave 1: the transaction's lists. Wave 2: contract metadata for the call target and for the
        emitters of the logs the events step will decode. Wave 3: implementations those name.
        Failures are kept by the client and surface in the step that needs them, as before.
        """
        pool = ThreadPoolExecutor(max_workers=PREFETCH_WORKERS)
        try:
            if tx.result != "awaiting_internal_transactions":  # _add_internal does not ask in that state
                # Not waited for: a slow internal call list (19 s on eth.blockscout.com for one old tx)
                # must not take the ABI lookups' time budget. Its step later joins the request in flight.
                pool.submit(self.explorer.internal_transactions, tx_hash)
            for future in [pool.submit(fetch, tx_hash) for fetch in (self.explorer.token_transfers, self.explorer.logs)]:
                future.exception()  # wait; errors stay in the client's memo
            if "explorer" not in self.cfg.abi_strategy.order:
                return
            primaries, known_impls = self._abi_addresses(tx_hash, tx)
            wave = {**known_impls, **primaries}  # lowercase -> address as the steps will spell it
            list(pool.map(lambda a: self._quietly(self.explorer.smart_contract, a), wave.values()))
            # abi_for follows the implementations named by the looked-up address itself, one level only
            more = {i.lower(): i for a in primaries.values() for i in self.explorer.implementations_of(a)}
            list(pool.map(lambda a: self._quietly(self.explorer.smart_contract, a),
                          [a for k, a in more.items() if k not in wave]))
        finally:
            pool.shutdown(wait=False)  # the internal call list may still be in flight

    @staticmethod
    def _quietly(fetch: Callable[[str], object], arg: str) -> None:
        try:
            fetch(arg)
        except Exception:
            pass  # kept in the client's memo; the step that needs it reports it

    def _abi_addresses(self, tx_hash: str, tx: Transaction) -> tuple[dict[str, str], dict[str, str]]:
        """(looked-up addresses, implementations already named for them) that the call and events steps
        will ask about, by the same rules as those steps. Keyed by lowercase (one request per contract,
        also when the payload spells an address in two casings); the value keeps the spelling used."""
        primaries: list[str] = []
        impls: list[str] = []
        to = tx.to
        if (to is not None and to.address and tx.raw_input not in (None, "0x") and (to.is_contract or to.implementations)
                and not self._is_native_contract(to.address)
                and self._delegations_applied(tx).get(to.address.lower()) != ZERO_ADDRESS):
            primaries.append(to.address)
            impls += list(to.implementations)
        try:
            transfers, _ = self.explorer.token_transfers(tx_hash)
            logs, _ = self.explorer.logs(tx_hash)
        except CollectorError:
            logs, transfers = [], []
        covered = {t.log_index for t in transfers if t.log_index is not None}
        for log in [lg for lg in logs if lg.index not in covered][:MAX_EVENTS]:
            if log.emitter is not None and log.emitter.address:
                primaries.append(log.emitter.address)
                impls += list(log.emitter.implementations)
        first = {}
        for a in primaries:
            if isinstance(a, str):
                first.setdefault(a.lower(), a)
        named = {}
        for a in impls:
            if isinstance(a, str) and a.lower() not in first:
                named.setdefault(a.lower(), a)
        return first, named

    def _describe_from_explorer(self, tx_hash: str, tx: Transaction, status: str) -> None:
        self.bundle.status = status
        self._safely("Transaction summary", lambda: self._add_overview(tx_hash, tx))
        if self.bundle.status in ("pending", "dropped"):
            return  # nothing below is final yet
        self._safely("Prefetch", lambda: self._prefetch(tx_hash, tx))
        self._safely("Fee", lambda: self._add_fee(tx_hash, tx))
        self._safely("Network-specific details", lambda: self._add_chain_facts(tx_hash, tx))
        self._safely("Revert reason", lambda: self._add_revert(tx_hash, tx))
        self._safely("Code delegations", lambda: self._add_authorizations(tx_hash, tx))
        self._safely("Call decoding", lambda: self._add_call(tx_hash, tx))
        self._safely("Source code", lambda: self._add_source_code(tx))
        self._safely("Function code", lambda: self._add_function_code(tx))
        covered_logs: set[int] = set()
        self._safely("Token transfers", lambda: covered_logs.update(self._add_transfers(tx_hash, tx)))
        self._safely("Internal calls", lambda: self._add_internal(tx_hash, tx))
        self._safely("Events", lambda: self._add_events(tx_hash, covered_logs))
        self._declare_undecoded_events()
        self._safely("Diagnosis", lambda: self._add_diagnosis(tx_hash, tx))
        self._safely("Reason in source", lambda: self._add_reason_in_source(tx))

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

    def _add_cross_check(self, view: RpcView) -> None:
        receipt = view.receipt
        if receipt is None or self.bundle.status not in ("success", "failed"):
            return
        if receipt.status == "unknown":
            return  # receipt without a status field: nothing to compare
        agree = receipt.status == self.bundle.status
        text = f"RPC receipt independently reports status {receipt.status}"
        text += ", matching the explorer." if agree else f", but the explorer says {self.bundle.status}."
        sources = [self._rpc_source(f"eth_getTransactionReceipt [{self.bundle.tx_hash}]"),
                   self._tx_source(self.bundle.tx_hash)]  # the check compares both: it cites both
        self.bundle.add("cross_check", text, sources, {"rpc_status": receipt.status, "agrees": agree},
                        confidence="confirmed")
        if not agree:
            self._gap("Status disagreement", "explorer and RPC report different statuses",
                      "Treat the RPC receipt as authoritative and re-check the explorer index", retryable=True,
                      cause="source_behind")


def build_bundle(tx_hash: str, cfg: AppConfig, explorer: ExplorerClient | None = None,
                 rpc: RpcClient | None = None) -> EvidenceBundle:
    """Collect everything about one transaction into a numbered, sourced bundle."""
    if not HASH_RE.match(tx_hash):
        raise InvalidHashError("Not a valid transaction hash (expected 0x followed by 64 hex characters).")
    budget = Budget(cfg.assistant.time_budget_s)
    explorer = explorer or ExplorerClient(cfg.explorer, budget=budget)
    rpc = rpc or RpcClient(cfg.rpc, budget=budget)
    return BundleBuilder(cfg, explorer, rpc).build(tx_hash)
