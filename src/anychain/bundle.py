"""Build the evidence bundle: deterministic, no LLM. Every fact gets an id and a source."""
import re

from anychain.collectors.explorer import MAX_PAGES, ExplorerClient
from anychain.collectors.http import CollectorError
from anychain.collectors.rpc import RpcClient
from anychain.config import AppConfig
from anychain.decoder import AbiDecoder
from anychain.models import EvidenceBundle, Gap, Source

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
MAX_EVENTS = 40
MAX_INTERNAL = 30
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _amount(raw: str | int, decimals: int) -> str:
    """Exact decimal string for an integer amount (no float rounding)."""
    n = int(raw)
    whole, frac = divmod(n, 10**decimals) if decimals else (n, 0)
    if not decimals or frac == 0:
        return str(whole)
    return f"{whole}.{str(frac).rjust(decimals, '0').rstrip('0')}"


def _addr(party: dict | str | None) -> str:
    """Address with its explorer label when there is one."""
    if not party:
        return "(none)"
    if isinstance(party, str):
        return party
    name = party.get("name") or party.get("ens_domain_name")
    return f"{party['hash']} ({name})" if name else party["hash"]


class BundleBuilder:
    def __init__(self, cfg: AppConfig, explorer: ExplorerClient, rpc: RpcClient):
        self.cfg, self.explorer, self.rpc = cfg, explorer, rpc
        self._abis: dict[str, AbiDecoder | None] = {}
        self._log_indexes: set = set()
        self.bundle: EvidenceBundle

    # ---- helpers -----------------------------------------------------
    def _api_source(self, path: str, label: str) -> Source:
        return Source(kind="explorer_api", label=label, url=self.explorer.url(path))

    def _decoder_for(self, address: str) -> AbiDecoder | None:
        key = address.lower()
        if key not in self._abis:
            found = self.explorer.abi_for(address)
            self._abis[key] = AbiDecoder(found[0]) if found else None
            self.bundle.abi_sources[address] = f"explorer: {found[1]}" if found else "none"
        return self._abis[key]

    def _gap(self, what: str, why: str, needed: str) -> None:
        self.bundle.gaps.append(Gap(what=what, why=why, needed=needed))

    # ---- main --------------------------------------------------------
    def build(self, tx_hash: str) -> EvidenceBundle:
        self.bundle = EvidenceBundle(network=self.cfg.network.name, tx_hash=tx_hash, status="unknown")
        tx = self._fetch_explorer_tx(tx_hash)
        rpc_view = self._fetch_rpc(tx_hash)
        if tx is None and rpc_view is None:
            return self.bundle
        if tx is not None:
            self._from_explorer(tx_hash, tx)
        else:
            self._from_rpc_only(tx_hash, rpc_view)
        if tx is not None and self.bundle.status == "pending":
            self.bundle.gaps = [g for g in self.bundle.gaps if g.what != "RPC transaction data"]
        if tx is not None and rpc_view is not None:
            self._cross_check(tx, rpc_view)
        return self.bundle

    def _fetch_explorer_tx(self, tx_hash: str) -> dict | None:
        try:
            return self.explorer.transaction(tx_hash)
        except CollectorError as exc:
            self._gap("Explorer transaction data", str(exc), "Explorer API reachable and indexing this transaction")
            return None

    def _fetch_rpc(self, tx_hash: str) -> dict | None:
        try:
            tx, receipt = self.rpc.transaction(tx_hash), self.rpc.receipt(tx_hash)
        except CollectorError as exc:
            self._gap("RPC transaction data", str(exc), "RPC endpoint reachable")
            return None
        if tx is None:
            self._gap("RPC transaction data", "RPC returned null for this hash", "Check the hash and that the RPC is on the right network")
            return None
        return {"tx": tx, "receipt": receipt}

    # ---- explorer path -----------------------------------------------
    def _from_explorer(self, tx_hash: str, tx: dict) -> None:
        cfg, b = self.cfg, self.bundle
        ui = Source(kind="explorer_ui", label="Explorer page", url=cfg.explorer.tx_url(tx_hash))
        api = self._api_source(f"/transactions/{tx_hash}", "Explorer API: transaction")
        status = {"ok": "success", "error": "failed"}.get(tx.get("status"), "pending" if tx.get("status") is None else "unknown")
        b.status = status
        sym, dec = cfg.network.native_symbol, cfg.network.native_decimals
        value = _amount(tx.get("value", 0), dec)
        if status == "pending":
            b.add("overview", f"Transaction is pending: not yet included in a block. From {_addr(tx.get('from'))} to {_addr(tx.get('to'))}. "
                  f"Native value offered: {value} {sym}. Nothing is final until it is mined.", [ui, api], {"status": status, "value": value})
            self._gap("Final outcome", "The transaction is still pending", "Wait for it to be mined (or replaced/dropped) and ask again")
            return
        verb = {"success": "succeeded", "failed": "failed (reverted)"}.get(status, f"has status {status}")
        b.add(
            "overview",
            f"Transaction {verb} in block {tx.get('block_number')} at {tx.get('timestamp')}. "
            f"From {_addr(tx.get('from'))} to {_addr(tx.get('to')) if tx.get('to') else '(contract creation)'}. "
            f"Native value sent: {value} {sym}. Gas used {tx.get('gas_used')} of limit {tx.get('gas_limit')}.",
            [ui, api],
            {"status": status, "block": tx.get("block_number"), "from": (tx.get("from") or {}).get("hash"),
             "to": (tx.get("to") or {}).get("hash"), "value": value, "gas_used": tx.get("gas_used"),
             "gas_limit": tx.get("gas_limit"), "timestamp": tx.get("timestamp")},
        )
        fee = (tx.get("fee") or {}).get("value")
        if fee is not None:
            b.add("fee", f"Fee paid: {_amount(fee, dec)} {sym}.", [api], {"fee": _amount(fee, dec)})
        if status == "failed" and tx.get("revert_reason"):
            b.add("revert", f"Explorer reports revert reason: {tx['revert_reason']!r}.", [api], {"revert_reason": tx["revert_reason"]})
        self._call_evidence(tx, api)
        self._transfers(tx_hash)
        self._internal(tx_hash)
        self._events(tx_hash)

    def _call_evidence(self, tx: dict, api: Source) -> None:
        b, sym = self.bundle, self.cfg.network.native_symbol
        data = tx.get("raw_input") or "0x"
        to = tx.get("to")
        if not to:
            b.add("call", "Contract creation transaction.", [api], {"created": tx.get("created_contract", {}).get("hash") if tx.get("created_contract") else None})
            return
        if data in ("0x", ""):
            b.add("call", f"Plain {sym} transfer (no call data) to {_addr(to)}.", [api])
            return
        decoder = self._decoder_for(to["hash"])
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            args = ", ".join(f"{a.name}={a.value}" for a in decoded.args)
            b.add("call", f"Called {decoded.signature} on {_addr(to)} with {args}. ABI source: {b.abi_sources[to['hash']]}.",
                  [api, Source(kind="explorer_api", label="Explorer API: contract ABI", url=self.explorer.url(f"/smart-contracts/{to['hash']}"))],
                  {"function": decoded.name, "args": {a.name: a.value for a in decoded.args}})
        else:
            b.add("call", f"Called an unknown function (selector {data[:10]}) on {_addr(to)}; could not decode.", [api], {"selector": data[:10]})
            self._gap("Call decoding", f"No ABI available for {to['hash']} (selector {data[:10]})",
                      "A verified contract on the explorer, or the contract ABI in a configured repo")

    def _transfers(self, tx_hash: str) -> None:
        path = f"/transactions/{tx_hash}/token-transfers"
        try:
            items, truncated = self.explorer.token_transfers(tx_hash)
        except CollectorError as exc:
            self._gap("Token transfers", str(exc), "Explorer API reachable")
            return
        src = self._api_source(path, "Explorer API: token transfers")
        self._log_indexes = set()
        for t in items:
            token, total = t.get("token") or {}, t.get("total") or {}
            self._log_indexes.add(t.get("log_index"))
            symbol = token.get("symbol") or token.get("name") or token.get("address_hash")
            if "value" in total and token.get("decimals") is not None:
                what = f"{_amount(total['value'], int(token['decimals']))} {symbol}"
            elif "token_id" in total:
                what = f"{symbol} token #{total['token_id']}"
            else:
                what = f"{symbol} (amount not reported)"
            self.bundle.add("token_transfer", f"Token transfer: {what} from {_addr(t.get('from'))} to {_addr(t.get('to'))}.", [src],
                            {"token": token.get("address_hash"), "from": (t.get("from") or {}).get("hash"), "to": (t.get("to") or {}).get("hash")})
        if truncated:
            self._gap("Token transfers", f"more than {MAX_PAGES} pages of transfers", "Open the explorer page for the full list")

    def _internal(self, tx_hash: str) -> None:
        try:
            items, truncated = self.explorer.internal_transactions(tx_hash)
        except CollectorError as exc:
            self._gap("Internal calls", str(exc), "Explorer API reachable")
            return
        src = self._api_source(f"/transactions/{tx_hash}/internal-transactions", "Explorer API: internal transactions")
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        reads = [it for it in items if it.get("type") == "staticcall"]
        items = [it for it in items if it.get("type") != "staticcall"]
        for it in items[:MAX_INTERNAL]:
            value = int(it.get("value") or 0)
            ok = "" if it.get("success", True) else " (failed)"
            if value > 0:
                text = f"Internal {sym} transfer of {_amount(value, dec)} {sym} from {_addr(it.get('from'))} to {_addr(it.get('to'))}{ok}."
            else:
                text = f"Internal {it.get('type')} from {_addr(it.get('from'))} to {_addr(it.get('to'))}{ok}."
            self.bundle.add("internal_call", text, [src], {"type": it.get("type"), "value": str(value)})
        if reads:
            self.bundle.add("internal_call", f"{len(reads)} read-only staticcall(s) (no state change) omitted from the list.", [src], {"staticcalls": len(reads)})
        if truncated or len(items) > MAX_INTERNAL:
            self._gap("Internal calls", f"showing the first {MAX_INTERNAL}", "Open the explorer page for the full list")

    def _events(self, tx_hash: str) -> None:
        try:
            items, truncated = self.explorer.logs(tx_hash)
        except CollectorError as exc:
            self._gap("Events", str(exc), "Explorer API reachable")
            return
        src = self._api_source(f"/transactions/{tx_hash}/logs", "Explorer API: logs")
        already = self._log_indexes
        shown = 0
        for log in items:
            if log.get("index") in already:  # token transfers already cover these
                continue
            if shown >= MAX_EVENTS:
                truncated = True
                break
            shown += 1
            address = log["address"]["hash"]
            topics = [t for t in log.get("topics", []) if t]
            decoder = self._decoder_for(address)
            ev = decoder.decode_log(topics, log.get("data") or "0x") if decoder else None
            if ev:
                args = ", ".join(f"{a.name}={a.value}" for a in ev.args)
                self.bundle.add("event", f"Event {ev.signature} emitted by {_addr(log['address'])}: {args}.", [src],
                                {"event": ev.name, "args": {a.name: a.value for a in ev.args}})
            else:
                self.bundle.add("event", f"Event with topic0 {topics[0] if topics else '(none)'} emitted by {_addr(log['address'])}; could not decode.", [src])
                self._gap("Event decoding", f"No ABI matches topic0 {topics[0] if topics else '?'} on {address}", "A verified contract on the explorer, or its ABI in a configured repo")
        if truncated:
            self._gap("Events", f"showing the first {MAX_EVENTS}", "Open the explorer page for the full list")

    # ---- rpc path ----------------------------------------------------
    def _from_rpc_only(self, tx_hash: str, view: dict) -> None:
        b, tx, receipt = self.bundle, view["tx"], view["receipt"]
        rpc_src = Source(kind="rpc", label="JSON-RPC", url=self.cfg.rpc.url, detail=f"eth_getTransactionByHash + eth_getTransactionReceipt [{tx_hash}]")
        status = "pending" if not receipt else ("success" if receipt.get("status") == "0x1" else "failed")
        b.status = status
        sym, dec = self.cfg.network.native_symbol, self.cfg.network.native_decimals
        b.add("overview", f"(From RPC only) Transaction {status} in block {int(tx['blockNumber'], 16) if tx.get('blockNumber') else 'pending'}. "
              f"From {tx['from']} to {tx.get('to') or '(contract creation)'}. Native value sent: {_amount(int(tx['value'], 16), dec)} {sym}. "
              f"Gas used {int(receipt['gasUsed'], 16) if receipt else 'n/a'} of limit {int(tx['gas'], 16)}.",
              [rpc_src], {"status": status, "from": tx["from"], "to": tx.get("to")})
        data = tx.get("input", "0x")
        if data not in ("0x", ""):
            b.add("call", f"Call data present (selector {data[:10]}); not decoded because the explorer, the ABI source, is unavailable.", [rpc_src], {"selector": data[:10]})
            self._gap("Call decoding", "Explorer unavailable, so no ABI could be fetched", "Explorer API reachable, or the contract ABI in a configured repo")
        if receipt:
            b.add("receipt", f"Receipt has {len(receipt.get('logs', []))} log(s).", [rpc_src])
            if receipt.get("logs"):
                self._gap("Event decoding", "Explorer unavailable, so events stay undecoded", "Explorer API reachable")

    def _cross_check(self, tx: dict, view: dict) -> None:
        receipt = view["receipt"]
        if not receipt:
            return
        rpc_status = "success" if receipt.get("status") == "0x1" else "failed"
        rpc_src = Source(kind="rpc", label="JSON-RPC", url=self.cfg.rpc.url, detail=f"eth_getTransactionReceipt [{self.bundle.tx_hash}]")
        agree = rpc_status == self.bundle.status
        text = f"RPC receipt independently reports status {rpc_status}" + (", matching the explorer." if agree else f", but the explorer says {self.bundle.status}.")
        self.bundle.add("cross_check", text, [rpc_src], {"rpc_status": rpc_status, "agrees": agree})
        if not agree:
            self._gap("Status disagreement", "Explorer and RPC report different statuses", "Treat the RPC receipt as authoritative and re-check the explorer index")



def build_bundle(tx_hash: str, cfg: AppConfig, explorer: ExplorerClient | None = None, rpc: RpcClient | None = None) -> EvidenceBundle:
    if not HASH_RE.match(tx_hash):
        raise ValueError("Not a valid transaction hash (expected 0x followed by 64 hex characters).")
    builder = BundleBuilder(cfg, explorer or ExplorerClient(cfg.explorer), rpc or RpcClient(cfg.rpc))
    return builder.build(tx_hash)
