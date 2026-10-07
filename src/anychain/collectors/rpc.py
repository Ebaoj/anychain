"""JSON-RPC collector: an independent view of the transaction, and a fallback."""
import time

import httpx

from anychain.collectors.http import RETRIES, Budget, CollectorError, make_client, request_json
from anychain.config import RpcConfig

# JSON-RPC error codes that mean "busy, try later" rather than "bad request".
BUSY_RPC_CODES = {-32005, 429}


class RpcBusyError(CollectorError):
    """The node answered but asked us to slow down."""


class RpcClient:
    def __init__(self, cfg: RpcConfig, client: httpx.Client | None = None, budget: Budget | None = None):
        self.cfg = cfg
        self.client = client or make_client(cfg.timeout_s)
        self.budget = budget

    def call(self, method: str, params: list) -> object:
        """One JSON-RPC call.

        Network trouble is already retried inside request_json. Here we only
        retry the node's own 'busy' answer, which arrives as a normal response.
        """
        for attempt in range(RETRIES + 1):
            try:
                return self._call_once(method, params)
            except RpcBusyError:
                if attempt == RETRIES:
                    raise
                wait = 0.5 * (attempt + 1)
                time.sleep(min(wait, self.budget.remaining()) if self.budget else wait)
        raise AssertionError("unreachable")

    def _call_once(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        payload = request_json(self.client, "POST", self.cfg.url, body=body,
                               timeout_s=self.cfg.timeout_s, budget=self.budget)
        if not isinstance(payload, dict):
            raise CollectorError(f"RPC {method} returned an unexpected payload", retryable=False)
        error = payload.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            if code in BUSY_RPC_CODES:
                raise RpcBusyError(f"RPC {method} busy: {error}")
            raise CollectorError(f"RPC {method} error: {error}", retryable=False)
        if "result" not in payload:
            raise CollectorError(f"RPC {method} answered without a result", retryable=False)
        return payload["result"]

    def transaction(self, tx_hash: str) -> dict | None:
        return _dict_or_none(self.call("eth_getTransactionByHash", [tx_hash]), "eth_getTransactionByHash")

    def receipt(self, tx_hash: str) -> dict | None:
        return _dict_or_none(self.call("eth_getTransactionReceipt", [tx_hash]), "eth_getTransactionReceipt")

    def code_at(self, address: str, block: int | str) -> str:
        """Contract code at `address` as of `block` or "latest" ('0x' means none). Old blocks need an archive node."""
        value = self.call("eth_getCode", [address, hex(block) if isinstance(block, int) else block])
        if not isinstance(value, str):
            raise CollectorError(f"RPC eth_getCode returned {value!r}", retryable=False)
        return value

    def chain_id(self) -> int:
        """The node's chain id. The standard is a hex string; some nodes send a plain number."""
        value = self.call("eth_chainId", [])
        try:
            if isinstance(value, int):
                return value
            text = str(value)
            return int(text, 16) if text.startswith("0x") else int(text)
        except ValueError:
            raise CollectorError(f"RPC eth_chainId returned {value!r}, not a chain id", retryable=False) from None


def _dict_or_none(value: object, method: str) -> dict | None:
    """null means "not found"; anything else that is not an object is a broken answer."""
    if value is None or isinstance(value, dict):
        return value
    raise CollectorError(f"RPC {method} returned {type(value).__name__}, expected an object", retryable=False)
