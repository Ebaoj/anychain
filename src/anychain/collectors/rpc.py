"""JSON-RPC collector: an independent view of the transaction, and a fallback."""
import time

import httpx

from anychain.collectors.http import RETRIES, Budget, CollectorError, make_client, request_json
from anychain.collectors.types import RpcReceipt, RpcTransaction
from anychain.config import RpcConfig

# JSON-RPC error codes that mean "busy, try later" rather than "bad request".
BUSY_RPC_CODES = {-32005, 429}


class RpcBusyError(CollectorError):
    """The node answered but asked us to slow down."""


class CallReverted(CollectorError):
    """eth_call ran and the contract reverted. `data` is the revert data (may be "0x" or None)."""

    def __init__(self, message: str, data: str | None):
        super().__init__(message, retryable=False)
        self.data = data


# How nodes say an eth_call reverted, each seen live on 2026-10-08 (all six configured nodes):
#   reth/op-reth/Tenderly/zkSync: code 3, "execution reverted" or "execution reverted: <reason>"
#   rskj (Rootstock): code -32015, "VM Exception while processing transaction: revert <reason>"
#                     or "... transaction reverted" when there is no reason
# The code alone is not proof: Gnosis's node (Tenderly) also uses code 3 for "intrinsic gas too low".
# So a revert needs the words. Note: zkSync answers "execution reverted" for an out-of-gas call too.
RPC_VM_EXCEPTION = "vm exception while processing transaction:"


def _is_revert(code: object, message: str) -> bool:
    text = message.lower()
    if text.startswith("execution reverted"):
        return True
    return code == -32015 and text.startswith(RPC_VM_EXCEPTION) and "revert" in text[len(RPC_VM_EXCEPTION):]


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
            message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
            if method == "eth_call" and _is_revert(code, message):
                data = error.get("data") if isinstance(error, dict) else None
                raise CallReverted(f"the call reverted: {message}", data if isinstance(data, str) else None)
            raise CollectorError(f"RPC {method} error: {error}", retryable=False)
        if "result" not in payload:
            raise CollectorError(f"RPC {method} answered without a result", retryable=False)
        return payload["result"]

    def transaction(self, tx_hash: str) -> RpcTransaction | None:
        found = _dict_or_none(self.call("eth_getTransactionByHash", [tx_hash]), "eth_getTransactionByHash")
        return RpcTransaction.from_rpc(found) if found is not None else None

    def receipt(self, tx_hash: str) -> RpcReceipt | None:
        found = _dict_or_none(self.call("eth_getTransactionReceipt", [tx_hash]), "eth_getTransactionReceipt")
        return RpcReceipt.from_rpc(found) if found is not None else None

    def code_at(self, address: str, block: int | str) -> str:
        """Contract code at `address` as of `block` or "latest" ('0x' means none). Old blocks need an archive node."""
        value = self.call("eth_getCode", [address, hex(block) if isinstance(block, int) else block])
        if not isinstance(value, str):
            raise CollectorError(f"RPC eth_getCode returned {value!r}", retryable=False)
        return value

    def eth_call(self, to: str, data: str, block: int | str, sender: str | None = None, value: int = 0) -> str:
        """Read-only call as of `block` (a number or "latest"). Returns the raw result.

        Raises CallReverted when the contract reverts, CollectorError for anything else the node says
        (e.g. a public node refusing old blocks: "Archive requests require a personal token").
        """
        call = {"to": to, "data": data}
        if sender:
            call["from"] = sender
        if value:
            call["value"] = hex(value)
        result = self.call("eth_call", [call, hex(block) if isinstance(block, int) else block])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise CollectorError(f"RPC eth_call returned {result!r}", retryable=False)
        return result

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
    """null means "not found"; an empty object or anything that is not an object is a broken answer."""
    if value is None:
        return None
    if isinstance(value, dict) and value:
        return value
    shape = "an empty object" if isinstance(value, dict) else type(value).__name__
    raise CollectorError(f"RPC {method} returned {shape}, expected a transaction or receipt", retryable=False)
