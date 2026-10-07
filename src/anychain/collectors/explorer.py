"""Blockscout API v2 collector. Endpoints: https://docs.blockscout.com/devs/apis/rest"""
import httpx

from anychain.collectors.http import CollectorError, get_json, make_client
from anychain.config import ExplorerConfig

MAX_PAGES = 5  # lists are paged; we stop here and declare the truncation


class ExplorerClient:
    def __init__(self, cfg: ExplorerConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client or make_client(cfg.timeout_s)

    def url(self, path: str) -> str:
        return f"{self.cfg.api_base}{path}"

    def _items(self, path: str) -> tuple[list[dict], bool]:
        """Follow pagination up to MAX_PAGES. Returns (items, truncated)."""
        items: list[dict] = []
        params: dict | None = None
        for _ in range(MAX_PAGES):
            page = get_json(self.client, self.url(path), params)
            items += page.get("items", [])
            params = page.get("next_page_params")
            if not params:
                return items, False
        return items, True

    def transaction(self, tx_hash: str) -> dict:
        return get_json(self.client, self.url(f"/transactions/{tx_hash}"))  # type: ignore[return-value]

    def logs(self, tx_hash: str) -> tuple[list[dict], bool]:
        return self._items(f"/transactions/{tx_hash}/logs")

    def token_transfers(self, tx_hash: str) -> tuple[list[dict], bool]:
        return self._items(f"/transactions/{tx_hash}/token-transfers")

    def internal_transactions(self, tx_hash: str) -> tuple[list[dict], bool]:
        return self._items(f"/transactions/{tx_hash}/internal-transactions")

    def smart_contract(self, address: str) -> dict:
        """Verified-contract metadata: abi, name, proxy info. 404 means not verified."""
        return get_json(self.client, self.url(f"/smart-contracts/{address}"))  # type: ignore[return-value]

    def abi_for(self, address: str) -> tuple[list[dict], str] | None:
        """ABI for an address, following proxies to the implementation.

        Returns (abi, description) or None when the contract is not verified.
        """
        try:
            meta = self.smart_contract(address)
        except CollectorError:
            return None
        abi = list(meta.get("abi") or [])
        note = f"verified contract {meta.get('name') or address}"
        for impl in meta.get("implementations") or []:
            impl_addr = impl.get("address_hash") or impl.get("address")
            if not impl_addr:
                continue
            try:
                impl_meta = self.smart_contract(impl_addr)
            except CollectorError:
                continue
            abi += impl_meta.get("abi") or []
            note += f" (proxy -> {impl_meta.get('name') or impl_addr})"
        return (abi, note) if abi else None
