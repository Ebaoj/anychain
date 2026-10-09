"""Blockscout API v2 collector. Endpoints: https://docs.blockscout.com/devs/apis/rest"""
import threading
from concurrent.futures import Future
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from anychain.collectors.http import Budget, CollectorError, NotFoundError, make_client, request_json
from anychain.collectors.types import AddressTransaction, InternalCall, Log, TokenTransfer, Transaction
from anychain.config import ExplorerConfig

MAX_PAGES = 5  # lists are paged; we stop here and declare the truncation


@dataclass
class AbiLookup:
    """Result of looking for a contract's ABI on the explorer."""

    abi: list[dict]
    note: str  # human description, e.g. "verified contract USDC (proxy -> FiatTokenV2_2)"
    failures: list[CollectorError] = field(default_factory=list)  # lookups that failed, not "unverified"


class ExplorerClient:
    def __init__(self, cfg: ExplorerConfig, client: httpx.Client | None = None, budget: Budget | None = None):
        self.cfg = cfg
        self.client = client or make_client(cfg.timeout_s)
        self.budget = budget
        # Each list and each contract is fetched once per client (one explanation), also when
        # prefetched in parallel: a second caller waits for the request in flight instead of making
        # its own. A failure is kept too, so it is not paid for twice.
        self._memo: dict[tuple, Future] = {}
        self._lock = threading.Lock()

    def _once(self, key: tuple, fetch: Callable[[], object]):
        with self._lock:
            future, owner = self._memo.get(key), False
            if future is None:
                future, owner = Future(), True
                self._memo[key] = future
        if owner:
            try:
                future.set_result(fetch())
            except BaseException as exc:  # also KeyboardInterrupt: waiters must never hang
                future.set_exception(exc)
        return future.result()

    def url(self, path: str) -> str:
        return f"{self.cfg.api_base}{path}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        data = request_json(self.client, "GET", self.url(path), params=params,
                            timeout_s=self.cfg.timeout_s, budget=self.budget)
        if not isinstance(data, dict):
            raise CollectorError(f"unexpected response shape from {self.url(path)}", retryable=False)
        return data

    def _items(self, path: str) -> tuple[list[dict], bool]:
        """Follow pagination up to MAX_PAGES. Returns (items, truncated)."""
        items: list[dict] = []
        params: dict | None = None
        for _ in range(MAX_PAGES):
            page = self._get(path, params)
            items += [i for i in page.get("items") or [] if isinstance(i, dict)]
            params = page.get("next_page_params")
            if not params:
                return items, False
        return items, True

    def transaction(self, tx_hash: str) -> Transaction:
        return Transaction.from_api(self._get(f"/transactions/{tx_hash}"))

    def address_transactions(self, address: str) -> list[AddressTransaction]:
        """The address's latest transactions, newest first (the first page only: triage lists a few, PHASE4 R1)."""
        page = self._get(f"/addresses/{address}/transactions")
        return [AddressTransaction.from_api(i) for i in page.get("items") or [] if isinstance(i, dict)]

    def logs(self, tx_hash: str) -> tuple[list[Log], bool]:
        def fetch():
            items, truncated = self._items(f"/transactions/{tx_hash}/logs")
            return [Log.from_api(i) for i in items], truncated
        return self._once(("logs", tx_hash), fetch)

    def token_transfers(self, tx_hash: str) -> tuple[list[TokenTransfer], bool]:
        def fetch():
            items, truncated = self._items(f"/transactions/{tx_hash}/token-transfers")
            return [TokenTransfer.from_api(i) for i in items], truncated
        return self._once(("token-transfers", tx_hash), fetch)

    def internal_transactions(self, tx_hash: str) -> tuple[list[InternalCall], bool]:
        def fetch():
            items, truncated = self._items(f"/transactions/{tx_hash}/internal-transactions")
            return [InternalCall.from_api(i) for i in items], truncated
        return self._once(("internal-transactions", tx_hash), fetch)

    def smart_contract(self, address: str) -> dict:
        """Contract metadata: abi (only when verified), name, proxy info."""
        return self._once(("smart-contract", address.lower()), lambda: self._get(f"/smart-contracts/{address}"))

    def implementations_of(self, address: str) -> list[str]:
        """Implementations named in already-fetched contract metadata (no request)."""
        with self._lock:
            future = self._memo.get(("smart-contract", address.lower()))
        done_ok = future is not None and future.done() and future.exception() is None
        meta = future.result() if done_ok and isinstance(future.result(), dict) else {}
        found = []
        for impl in meta.get("implementations") or []:
            impl_address = impl.get("address_hash") or impl.get("address") if isinstance(impl, dict) else None
            if isinstance(impl_address, str):
                found.append(impl_address)
        return found

    def abi_for(self, address: str, known_implementations: list[str] | None = None) -> AbiLookup:
        """ABI for an address, following proxies to their implementation.

        `known_implementations` comes from the transaction payload; it lets an
        unverified proxy still be decoded through its verified implementation.
        """
        abi: list[dict] = []
        names: list[str] = []
        failures: list[CollectorError] = []
        implementations = [a for a in known_implementations or [] if isinstance(a, str)]

        meta = self._lookup(address, failures)
        if meta is not None:
            if _abi_entries(meta):
                abi += _abi_entries(meta)
                names.append(meta.get("name") or address)
            for impl in meta.get("implementations") or []:
                impl_address = impl.get("address_hash") or impl.get("address") if isinstance(impl, dict) else None
                if isinstance(impl_address, str):
                    implementations.append(impl_address)

        seen: set[str] = set()
        for impl_address in implementations:
            if impl_address.lower() in seen:
                continue
            seen.add(impl_address.lower())
            impl_meta = self._lookup(impl_address, failures)
            if impl_meta is not None and _abi_entries(impl_meta):
                abi += _abi_entries(impl_meta)  # added last: wins on duplicate selectors
                names.append(f"proxy -> {impl_meta.get('name') or impl_address}")

        note = f"verified contract {' '.join(names)}" if names else "no verified ABI"
        return AbiLookup(abi=abi, note=note, failures=failures)

    def _lookup(self, address: str, failures: list[CollectorError]) -> dict | None:
        """One /smart-contracts call. 'Not verified' is normal; other failures are recorded.

        Blockscout answers 200 without an `abi` for unverified contracts, and 404
        for addresses it does not know as contracts. Both mean "no ABI here".
        """
        try:
            return self.smart_contract(address)
        except NotFoundError:
            return None
        except CollectorError as exc:
            failures.append(exc)
            return None


def _abi_entries(meta: dict) -> list[dict]:
    """The ABI as a list of entries, or [] when missing or malformed."""
    abi = meta.get("abi")
    return [e for e in abi if isinstance(e, dict)] if isinstance(abi, list) else []
