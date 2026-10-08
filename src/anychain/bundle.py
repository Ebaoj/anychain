"""Build the evidence bundle: deterministic, no LLM. Every fact gets an id and a source.

Reading order: `build()` runs the steps; each `_add_*` method adds facts for one topic.
Each step is wrapped by `_safely()`, so an unexpected payload costs one topic
(declared as a gap), never the whole answer. Explorer and RPC answers arrive as typed
objects (collectors/types.py); network-type specifics come from a profile (chains.py).
"""
import re

import httpx
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from eth_utils import to_checksum_address

from anychain.chains import BLOCKSCOUT_CHAIN_TYPES, ChainGap, Fee, FeePart, TokenRef, audit_fields, profile_for
from anychain.collectors.explorer import MAX_PAGES, ExplorerClient
from anychain.collectors.http import Budget, CollectorError, NotFoundError
from anychain.collectors.rpc import InsufficientFunds, RpcClient
from anychain.collectors.types import (
    AddressRef, Authorization, InternalCall, Log, RevertReason, RpcReceipt, RpcTransaction, TokenTransfer, Transaction,
    to_int,
)
from anychain.config import AppConfig
from eth_utils import keccak

from anychain.collectors.signatures import SignatureDb
from anychain.decoder import AbiDecoder, decode_revert, fit_signature
from anychain.collectors.repo import Repo, RepoCache
from anychain.diagnosis import Context, diagnose, reason_text
from anychain.solidity import SolidityIndex, filter_abi
from anychain.reads import REPLAY_LIMITS, StateReader
from anychain.models import EvidenceBundle, GapCause, Source

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_EVENTS = 40
REPLAY_WHEN = {"no_reason", "generic_failure", "possibly_out_of_gas"}  # findings without the explorer's reason
MAX_EVENT_LOOKUPS = 10  # distinct undecoded event topics looked up per answer
MAX_PLACES = 3  # places a reason text is listed at, per source; the rest are counted
DEPOSIT_TX_TYPES = {0x7E, 0xFF}  # OP Stack deposits and zkSync L1->L2 priority txs: funds minted as they run
# What a replayed reason means, per diagnosis rule, worded about the replay (D32).
REPLAY_MEANING = {
    "insufficient_balance": "the standard ERC-20 message for a transfer larger than the balance it moves from",
    "insufficient_allowance": "the standard ERC-20 message for moving tokens beyond an approval",
    "paused": "the reason a paused contract gives",
    ("slippage", "single_source"): "the slippage check of Uniswap's routers (the swap would not meet the "
                                    "sender's limit)",
    ("slippage", "candidate"): "the words of the slippage check of Uniswap's routers; this contract's code decides "
                               "what they mean",
    ("deadline", "single_source"): "Uniswap's deadline check (included after the deadline set in it)",
    ("deadline", "candidate"): "a reason that mentions a time limit; which one is defined in the contract's code",
    "contract_reason": "the contract's own reason; what it means is defined in its code",
}
PREFETCH_WORKERS = 6  # parallel explorer requests while prefetching (D26)
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
        self.rpc_view: RpcView | None = None
        self.repos: list[Repo] | None = None  # configured repos from the local cache, loaded on first use
        self.source_repo: Repo | None = None  # the repo matched to the called contract, if any
        self.abi_origin: dict[str, tuple] = {}  # address -> (repo_pinned | repo_match, repo, contract) when not the explorer
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
        tx = self._fetch_explorer_tx(tx_hash)
        explorer_status = self._status_from_explorer(tx) if tx is not None else None
        rpc_view = self.rpc_view = self._fetch_rpc(tx_hash, explorer_status)

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
        covered_logs: set[int] = set()
        self._safely("Token transfers", lambda: covered_logs.update(self._add_transfers(tx_hash, tx)))
        self._safely("Internal calls", lambda: self._add_internal(tx_hash, tx))
        self._safely("Events", lambda: self._add_events(tx_hash, covered_logs))
        self._declare_undecoded_events()
        self._safely("Diagnosis", lambda: self._add_diagnosis(tx_hash, tx))
        self._safely("Reason in source", lambda: self._add_reason_in_source(tx))

    # ---- source code from configured repos and the explorer's verified source (PHASE2 T6, D33) ----

    def _loaded_repos(self) -> list[Repo]:
        if self.repos is None:
            self.repos, cache = [], RepoCache(self.cfg.storage.cache_dir)
            for repo_cfg in self.cfg.repos:
                try:
                    repo = cache.load(repo_cfg)
                except CollectorError as exc:
                    self._gap("Source code", str(exc), "A GitHub repository URL in the config", retryable=False,
                              cause="config_error")
                    continue
                except Exception as exc:  # e.g. a damaged cache: this repo only, the others still load
                    self._gap("Source code", f"the repository {repo_cfg.url} could not be read from the cache "
                              f"({type(exc).__name__}: {exc})", "Run anychain repos sync again", retryable=False,
                              cause="processing_error")
                    continue
                if repo is None:
                    self._gap("Source code", f"the configured repository {repo_cfg.url} is not in the local cache",
                              "Run once: anychain repos sync --config <this network's config>", retryable=False,
                              cause="config_error")
                else:
                    self.repos.append(repo)
        return self.repos

    def _verified(self, address: str) -> tuple[str | None, SolidityIndex | None]:
        """(contract name, index of its verified source files) from the explorer's metadata, built once."""
        key = address.lower()
        if key not in self.verified_cache:
            self.verified_cache[key] = self._read_verified(address)
        return self.verified_cache[key]

    def _contract_name(self, address: str) -> str | None:
        """The name the explorer gives a contract's verified code (no source indexing)."""
        name = self.explorer.smart_contract(address).get("name")
        return name if isinstance(name, str) else None

    def _read_verified(self, address: str) -> tuple[str | None, SolidityIndex | None]:
        meta = self.explorer.smart_contract(address)  # fetched once per explanation (memoized)
        files = {}
        if isinstance(meta.get("source_code"), str) and meta.get("file_path"):
            files[str(meta["file_path"])] = meta["source_code"]
        for extra in meta.get("additional_sources") or []:
            if isinstance(extra, dict) and isinstance(extra.get("source_code"), str) and extra.get("file_path"):
                files[str(extra["file_path"])] = extra["source_code"]
        name = meta.get("name") if isinstance(meta.get("name"), str) else None
        return name, (SolidityIndex(files) if files else None)

    def _contracts_behind(self, to: AddressRef) -> list[str]:
        """The call target's code addresses: its implementations first (what runs), then itself. Empty for an
        account the explorer does not list as a contract (no metadata request for it)."""
        if not (to.is_contract or to.implementations):
            return []
        return [a for a in [*to.implementations, to.address] if isinstance(a, str) and a]

    def _add_source_code(self, tx: Transaction) -> None:
        if (not self.cfg.repos or "explorer" not in self.cfg.abi_strategy.order or not self.explorer_answered
                or tx.to is None or not tx.to.address):
            return
        repos = self._loaded_repos()
        data = tx.raw_input or ""
        if not repos or len(data) < 10:
            return
        selector = data[:10].lower()
        for address in self._contracts_behind(tx.to):
            try:
                name = self._contract_name(address)
            except CollectorError:
                continue
            if not name or not any(name in r.index.contracts or name in r.index.ambiguous for r in repos):
                continue
            _name, verified = self._verified(address)  # its source is indexed only now that a repo has the name
            for repo in repos:
                if name and name in repo.index.ambiguous:
                    self._gap("Source code", f"{name} is declared in more than one file of {repo.label}, so which one "
                              "is the deployed contract cannot be told", "A config naming the file (not supported yet)",
                              retryable=False, cause="not_interpretable")
                    return
                if name and name in repo.index.contracts:
                    self.source_repo = repo
                    self._cite_function(repo, name, selector, address, verified)
                    return

    def _cite_function(self, repo: Repo, name: str, selector: str, address: str,
                       verified: SolidityIndex | None) -> None:
        found = repo.index.find(name, selector)
        in_verified = verified.find(name, selector) if verified else None
        api = self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")
        if verified is None:
            compare = ("The explorer has no verified source for this contract, so whether the deployed code is this "
                       "version is not known: the match is by the contract's name.")
        elif in_verified is None:
            compare = ("The explorer's verified source does not define a function with this selector under this "
                       "contract's name, so the two copies could not be compared.")
        if found is None:
            contract = repo.index.contracts[name]
            outside = repo.index.external_bases(name)
            text = (f"{name} is in the configured repository {repo.label}, but no implemented external or public "
                    f"function with selector {selector} was found in its files for {name} or its bases there")
            text += f" (its bases outside the repository: {', '.join(outside)})." if outside else "."
            sources = [Source(kind="repo", label=f"Repository {repo.label}",
                              url=repo.permalink(contract.path, contract.start, contract.end))]
            if in_verified:
                f, c = in_verified.function, in_verified.contract
                text += (f" The deployed code's verified source on the explorer defines {f.signature} in {c.name} "
                         f"({c.path}, lines {f.start}-{f.end})")
                text += (", a contract this repository does not have, so its code is not the deployed code."
                         if c.name not in repo.index.contracts else ".")
                sources.append(api)
            self.bundle.add("source", text, sources, {"repo": repo.label, "contract": name, "selector": selector,
                                                      "in_repo": False}, confidence="single_source")
            return
        f, c = found.function, found.contract
        text = (f"In the configured repository {repo.label}, {f.signature} of {name} is defined in {c.name} "
                f"({c.path}, lines {f.start}-{f.end}).")
        sources = [Source(kind="repo", label=f"Repository {repo.label}", url=repo.permalink(c.path, f.start, f.end))]
        same = None
        if in_verified:
            same = in_verified.function.text == f.text
            vc = in_verified.contract
            text += (" Its text is the same in the contract's verified source on the explorer (comments and spacing "
                     "aside)." if same else
                     f" Its text differs from the contract's verified source on the explorer ({vc.path}, lines "
                     f"{in_verified.function.start}-{in_verified.function.end}): the repository's code is not the "
                     "deployed code (a different version, or a different contract with the same name).")
            sources.append(api)
            confidence = "single_source"
        else:
            text += " " + compare
            confidence = "candidate"
        self.bundle.add("source", text, sources,
                        {"repo": repo.label, "contract": name, "defined_in": c.name, "path": c.path,
                         "lines": [f.start, f.end], "selector": selector, "in_repo": True, "same_as_verified": same},
                        confidence=confidence)

    def _add_reason_in_source(self, tx: Transaction) -> None:
        """Where the failure's reason text is written: in the verified source of the contract called, and in
        the configured repo matched to that contract (not in unrelated repos)."""
        if self.bundle.status != "failed":
            return
        text = reason_text(tx.revert_reason)
        if not text:
            return
        places: list[tuple[str, Source]] = []
        found_total = 0
        if self.source_repo is not None:
            hits = self.source_repo.index.literal(text)
            found_total += len(hits)
            for path, line in hits[:MAX_PLACES]:
                places.append((f"{self.source_repo.label} {path}, line {line}",
                               Source(kind="repo", label=f"Repository {self.source_repo.label}",
                                      url=self.source_repo.permalink(path, line, line))))
        if tx.to is not None and self.explorer_answered and "explorer" in self.cfg.abi_strategy.order:
            for address in self._contracts_behind(tx.to)[:1]:
                try:
                    _name, verified = self._verified(address)
                except CollectorError:
                    verified = None
                hits = verified.literal(text) if verified else []
                found_total += len(hits)
                for path, line in hits[:MAX_PLACES]:
                    places.append((f"the verified source {path}, line {line}",
                                   self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")))
        if places:
            more = found_total - len(places)
            sources = list({(s.kind, s.url): s for _, s in places}.values())  # one source per page
            self.bundle.add("source", f"The reason {text!r} is written in " + "; ".join(p for p, _ in places)
                            + (f" (and {more} more place{'s' if more > 1 else ''})." if more > 0 else "."),
                            sources, {"reason": text, "places": [p for p, _ in places]}, confidence="single_source")

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
                      explorer_text=None if generic else explorer_text)
        finding = diagnose(ctx)
        read_ids = []
        for read in finding.reads:
            fact = self.bundle.add("state_read", f"At block {read.block}, {read.signature.split('(')[0]} on "
                                   f"{read.contract} returned {read.value}.",
                                   [self._rpc_source(read.detail)], {"call": read.detail, "value": str(read.value)})
            read_ids.append(fact.id)
        sources = [self._tx_source(tx_hash)] + [self._rpc_source(r.detail) for r in finding.reads]
        if generic:
            sources.append(Source(kind="repo", label="Network software source", url=generic[1]))
        sources += [Source(kind="repo", label="Source of the rule's meaning", url=u) for u in finding.source_urls]
        steps = " ".join(f"Next step: {s}" for s in finding.next_steps)
        self.bundle.add("diagnosis", f"{finding.text} {steps}", sources,
                        {"rule": finding.rule, "level": finding.level, "reads": read_ids,
                         "next_steps": finding.next_steps},
                        confidence="confirmed" if finding.level == "confirmed" else finding.level)
        for missing in finding.missing:
            self._gap("Diagnosis", missing.why, missing.needed, retryable=missing.retryable, cause=missing.cause)
        if finding.rule in REPLAY_WHEN and ctx.reader is not None:
            self._add_replay(tx_hash, tx, ctx)

    def _add_replay(self, tx_hash: str, tx: Transaction, ctx: Context) -> None:
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
        else:
            sender, to, data, value, gas = ctx.sender, ctx.to, tx.raw_input, tx.value or 0, tx.gas_limit
            sources = [self._rpc_source(f"eth_call replay of {tx_hash} from {sender}"), self._tx_source(tx_hash)]
        block = tx.block_number
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
        if decoded.kind == "custom" and origin == "repo_pinned":
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

    def _replay_skip(self, tx: Transaction) -> str | None:
        """Why a replay of this transaction would not reproduce its call (None: it can be tried)."""
        if tx.to is None or not address_of(tx.to):
            return "it is a contract creation, which a call cannot repeat"
        if to_int(tx.raw.get("type")) in DEPOSIT_TX_TYPES:
            return ("it is a deposit from L1, whose funds the network adds as it runs; a replay cannot include "
                    "that")
        if tx.authorizations or (self.rpc_view is not None and self.rpc_view.tx.delegates):
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
        self.bundle.add("diagnosis", f"Possible cause, from the replay ({fact.id}): the replay reverted with "
                        f"{decoded.text}, {meaning}. If the original transaction failed the same way, that is its "
                        "cause. " + " ".join(f"Next step: {s}" for s in steps),
                        sources + [Source(kind="repo", label="Source of the rule's meaning", url=u) for u in urls],
                        {"rule": "replay", "level": "candidate", "from_replay": True, "replay": fact.id, "reads": [],
                         "next_steps": steps}, confidence="candidate")

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
            self._safely("Signature database", lambda: self._add_signature_candidates("call", data[:10], data))
            return
        abi_source, confidence = self._abi_provenance(to.address)
        b.add("call", self._call_text(to.address, decoded, self._party(to)), [source, abi_source],
              {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}}, confidence=confidence)

    def _abi_provenance(self, address: str) -> tuple[Source, str | None]:
        """The ABI's source for a decoded fact, and the confidence it allows (None: follow the sources)."""
        origin = self.abi_origin.get(address.lower())
        if origin is None:
            return self._api_source(f"/smart-contracts/{address}", "Explorer API: contract ABI"), None
        kind, repo, contract = origin
        self.abi_notes[address.lower()] = self.repo_notes[address.lower()]  # used for this fact: now it is the source
        self.bundle.abi_sources[address] = self.repo_notes[address.lower()]
        if kind == "repo_pinned":
            c = repo.index.contracts[contract]
            return Source(kind="repo", label=f"Repository {repo.label}", url=repo.permalink(c.path, c.start, c.end)), None
        return Source(kind="repo", label=f"Repository {repo.label if repo else 'source signatures'}",
                      url=repo.tree_url if repo else None), "candidate"

    def _call_text(self, address: str, decoded, party: str) -> str:
        if self.abi_origin.get(address.lower(), ("",))[0] == "repo_match":
            return (f"The call's selector matches {decoded.signature} in a configured repository; decoded with it, the "
                    f"call on {party} would be{with_args(decoded.args) or ' without arguments'}. Not confirmed: "
                    f"{self.abi_notes[address.lower()]}.")
        return f"Called {decoded.signature} on {party}{with_args(decoded.args)}. ABI source: {self.abi_notes[address.lower()]}."

    def _add_native_contract_call(self, to_text: str, address: str, data: str, source: Source) -> None:
        """A call to a contract built into the node: it runs without bytecode, but it can have a
        published ABI (e.g. Rootstock's Bridge), so the normal ABI lookup still applies."""
        decoder = self._decoder_for(address)
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            abi_source, confidence = self._abi_provenance(address)
            self.bundle.add("call", self._call_text(address, decoded, to_text), [source, abi_source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args},
                             "native_contract": True}, confidence=confidence)
            return
        self.bundle.add("call", f"Called {to_text}, a contract built into the network's node, with selector "
                        f"{data[:10]}; not decoded.", [source], {"selector": data[:10], "native_contract": True})
        self._declare_undecoded("Call decoding", address, f"selector {data[:10]}")

    def _decoder_for(self, address: str, implementations: list[str] | None = None) -> AbiDecoder | None:
        """ABI decoder for a contract, cached per run. Records where the ABI came from."""
        key = address.lower()
        if key in self.decoders:
            return self.decoders[key]
        explorer_on = "explorer" in self.cfg.abi_strategy.order
        decoder, note = None, ("none (explorer unavailable)" if explorer_on else "none (explorer ABI disabled in config)")
        if explorer_on and self.explorer_answered:
            lookup = self.explorer.abi_for(address, implementations)
            decoder = AbiDecoder(lookup.abi) if lookup.abi else None
            note = f"explorer: {lookup.note}" if decoder else "none"
            if lookup.failures:
                retryable = any(f.retryable for f in lookup.failures)
                self.abi_lookup_failed[key] = retryable
                self._gap("ABI lookup", f"the ABI lookup for {address} failed: {lookup.failures[0]}",
                          "Try again in a few minutes" if retryable else "Check the contract on the explorer page",
                          retryable, _source_cause(retryable))
        if decoder is None:
            repo_decoder = self._repo_decoder(address)
            if repo_decoder:  # its note is recorded only when it decodes something (_abi_provenance)
                decoder, self.repo_notes[key] = repo_decoder
        self.decoders[key] = decoder
        self.abi_notes[key] = note
        self.bundle.abi_sources[address] = note
        return decoder

    def _add_signature_candidates(self, what: str, key: str, data: str | None) -> None:
        """Public signature database candidates for a selector or event topic no ABI decodes (PHASE2 T7, D35).
        Always a candidate: anyone can add entries and different signatures share selectors. A function or
        error signature is offered only when the data fits its types exactly."""
        db = self.signatures
        if db is None or not db.enabled or db.failed:  # after one failure: no more lookups, no more gaps
            return
        try:
            names = db.events(key) if what == "event" else db.functions(key)
        except CollectorError as exc:
            self._gap("Signature database", f"the lookup of {key} failed: {exc}",
                      "Try again in a few minutes" if exc.retryable else "Check the signature database URL in the config",
                      retryable=exc.retryable, cause=_source_cause(exc.retryable))
            return
        if not names:
            return
        host = httpx.URL(db.cfg.url).host
        source = Source(kind="signature_db", label=f"Signature database {host}",
                        url=f"{db.cfg.url.rstrip('/')}/{'event-signatures' if what == 'event' else 'signatures'}/"
                            f"?hex_signature={key}")
        caveat = ("Anyone can add entries there and different signatures can share a selector, so this is not "
                  "confirmed.")
        thing = {"call": "The call's selector", "error": "The replay's custom error selector",
                 "event": "The event's topic"}[what]
        if what == "event":
            # An event topic is the 32-byte hash of its signature: a text whose hash equals the topic is proven to
            # be that signature (finding another text with the same hash is not feasible). Others are discarded.
            proven = [n for n in names if "0x" + keccak(text=n).hex() == key.lower()]
            if proven:
                self.bundle.add("event_signature",
                                f"The event's topic {key[:10]}… is the hash of {proven[0]}: that text comes from the "
                                f"public signature database {host}, and its hash was checked to equal the topic. Its "
                                "arguments are not decoded: which of them are indexed is not known.",
                                [source], {"topic": key, "signature": proven[0]}, confidence="single_source")
            return
        fitting = [(n, args) for n in names if (args := fit_signature(n, data or "")) is not None]
        if not fitting:
            text = (f"{thing} {key} has entries in the public signature database {host} ({self._and_list(names)}), "
                    "but the data does not fit their types exactly, so none of them is offered.")
            self.bundle.add("candidate", text, [source], {"selector": key, "signatures": names, "fitting": []},
                            confidence="candidate")
            return
        if len(fitting) == 1:
            name, args = fitting[0]
            shown = ", ".join(f"{a.name}={a.value}" for a in args) or "no arguments"
            text = (f"{thing} {key} matches {name} in the public signature database {host}, and the data fits its "
                    f"types exactly; decoded with it: {shown}. {caveat}")
        else:
            text = (f"{thing} {key} matches several signatures in the public signature database {host} that all fit "
                    f"the data: {self._and_list([n for n, _ in fitting])}. Which one, if any, is not known. {caveat}")
        self.bundle.add("candidate", text, [source],
                        {"selector": key, "signatures": names, "fitting": [n for n, _ in fitting]}, confidence="candidate")

    @staticmethod
    def _and_list(items: list[str]) -> str:
        return items[0] if len(items) == 1 else "; ".join(items[:-1]) + "; or " + items[-1]

    def _repo_decoder(self, address: str) -> tuple[AbiDecoder, str] | None:
        """No ABI from the explorer: decode with the configured repos' source (PHASE2 T6, D34).
        A contract pinned in address_map uses its own ABI from source (single source: the config says which
        contract it is); otherwise every selector the repos declare once is tried, and a match is a candidate."""
        order = self.cfg.abi_strategy.order
        if not self.cfg.repos or "repo_source_signatures" not in order:
            return None
        repos = self._loaded_repos()
        pin = self.cfg.address_map.get(address.lower())
        if pin is not None:
            repo = next((r for r in repos if r.url == pin.repo), None)
            problem = ("the repository is not synced" if repo is None else
                       f"{pin.contract} is declared in more than one of its files" if pin.contract in repo.index.ambiguous
                       else f"it has no contract named {pin.contract}" if pin.contract not in repo.index.contracts
                       else None)
            abi = repo.index.abi(pin.contract) if problem is None else []
            if problem is None and not abi:
                problem = f"no ABI could be built from {pin.contract}'s source (unresolved types or inheritance)"
            if problem:
                self._gap("ABI lookup", f"address_map pins {address} to {pin.contract} in {pin.repo}, but {problem}",
                          "Sync the repo, or fix the contract name in address_map", retryable=False,
                          cause="config_error")
                return None
            self.abi_origin[address.lower()] = ("repo_pinned", repo, pin.contract)
            return AbiDecoder(abi), f"repository {repo.label}: {pin.contract} (pinned to this address in the config)"
        abi = filter_abi([e for r in repos for e in r.index.abi()])  # ambiguity across repos too
        if not abi:
            return None
        self.abi_origin[address.lower()] = ("repo_match", repos[0] if len(repos) == 1 else None, None)
        labels = ", ".join(r.label for r in repos)
        return AbiDecoder(abi), (f"repository {labels} source signatures (a selector match in the repository; "
                                 "which contract this is was not confirmed)")

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
                            {"type": it.type, "value": str(it.value), "moves_value": moves_value, "same_as": same_as})
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
                text = (f"Event {event.signature} emitted by {self._party(emitter)}{args}." if origin[0] == "repo_pinned"
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


class UnreadableLog(ValueError):
    """The explorer sent a log this tool cannot read (a source problem, not a defect here)."""


def _failure_note(it: InternalCall, short: bool = False) -> str:
    """Why an internal call has no effect, in the explorer's terms (values seen in real recordings)."""
    if it.error == "Parent reverted":  # the call itself may have succeeded; a caller above it reverted
        return "undone because a call above it reverted" if short else "but was undone because a call above it reverted"
    if it.error == "Reverted":
        return "this internal call reverted" if short else "but reverted"
    reason = f": the explorer reports {it.error!r}" if it.error else ""
    return f"this internal call failed{reason}" if short else f"but failed{reason}"


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
