"""JSON-RPC collector: an independent view of the transaction, and a fallback."""
import httpx

from anychain.collectors.http import CollectorError, make_client
from anychain.config import RpcConfig


class RpcClient:
    def __init__(self, cfg: RpcConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client or make_client(cfg.timeout_s)

    def call(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = self.client.post(self.cfg.url, json=body)
        except httpx.HTTPError as exc:
            raise CollectorError(f"RPC {method} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise CollectorError(f"RPC {method} returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CollectorError(f"RPC {method} failed: {type(exc).__name__}: {exc}") from exc
        if "error" in payload:
            raise CollectorError(f"RPC {method} error: {payload['error']}")
        return payload.get("result")

    def transaction(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionByHash", [tx_hash])  # type: ignore[return-value]

    def receipt(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", [tx_hash])  # type: ignore[return-value]

    def eth_call(self, to: str, data: str, block: int | str = "latest", sender: str | None = None) -> str:
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        tag = hex(block) if isinstance(block, int) else block
        return self.call("eth_call", [tx, tag])  # type: ignore[return-value]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)  # type: ignore[arg-type]
