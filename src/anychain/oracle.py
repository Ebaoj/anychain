"""Independent checks of an answer against the network's JSON-RPC node.

Used by the acceptance run (docs/ACCEPTANCE.md) and, later, the eval. The node is a source
neither the explorer nor this tool's interpretation controls, so a mismatch here is either a
false fact or a limit of the check; every mismatch is reviewed by hand.

Each check returns pass / fail / skip (skip = this check cannot judge this transaction, with why).
"""
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from eth_abi import decode
from eth_utils import event_signature_to_log_topic

from anychain.config import AppConfig
from anychain.models import EvidenceBundle


def _topic(signature: str) -> str:
    return "0x" + event_signature_to_log_topic(signature).hex()


TRANSFER = _topic("Transfer(address,address,uint256)")  # ERC-20 (2 indexed) and ERC-721 (3 indexed)
TRANSFER_SINGLE = _topic("TransferSingle(address,address,address,uint256,uint256)")  # ERC-1155
TRANSFER_BATCH = _topic("TransferBatch(address,address,address,uint256[],uint256[])")  # ERC-1155, one entry per id
WETH_DEPOSIT = _topic("Deposit(address,uint256)")  # WETH-style wrap, listed by explorers as a mint
WETH_WITHDRAWAL = _topic("Withdrawal(address,uint256)")  # WETH-style unwrap, listed as a burn
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
ZERO = "0x" + "0" * 40


@dataclass
class Check:
    name: str
    status: str  # pass | fail | skip
    detail: str = ""


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return None
    return None


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _raw_units(text: str, decimals: int) -> int | None:
    """'0.0001' with 18 decimals -> 100000000000000 (exact, no floats)."""
    try:
        return int(Decimal(text) * (Decimal(10) ** decimals))
    except Exception:
        return None


def receipt_transfers(receipt: dict, skip_tokens: set[str]) -> list[tuple[str, str, str, str]]:
    """(token, from, to, amount-or-id) for every token movement the receipt's logs prove.

    Mirrors what an explorer lists as token transfers: ERC-20/721 Transfer, ERC-1155
    TransferSingle, and WETH-style Deposit/Withdrawal (shown as mint/burn).
    """
    found, wraps = [], []
    for log in receipt.get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        token, data = (log.get("address") or "").lower(), log.get("data") or "0x"
        if not topics or token in skip_tokens:
            continue
        words = [data[2 + i:66 + i] for i in range(0, len(data) - 2, 64)]
        if topics[0] == TRANSFER and len(topics) == 3 and len(words) == 1:
            found.append((token, _topic_address(topics[1]), _topic_address(topics[2]), str(int(words[0], 16))))
        elif topics[0] == TRANSFER and len(topics) == 4:
            found.append((token, _topic_address(topics[1]), _topic_address(topics[2]), str(int(topics[3], 16))))
        elif topics[0] == TRANSFER_SINGLE and len(topics) == 4 and len(words) == 2:
            # same "quantity x id" form the tool's data uses
            found.append((token, _topic_address(topics[2]), _topic_address(topics[3]), f"{int(words[1], 16)}x{int(words[0], 16)}"))
        elif topics[0] == TRANSFER_BATCH and len(topics) == 4:
            ids, values = decode(["uint256[]", "uint256[]"], bytes.fromhex(data[2:]))
            for token_id, value in zip(ids, values):
                found.append((token, _topic_address(topics[2]), _topic_address(topics[3]), f"{value}x{token_id}"))
        elif topics[0] == WETH_DEPOSIT and len(topics) == 2 and len(words) == 1:
            wraps.append((token, ZERO, _topic_address(topics[1]), str(int(words[0], 16))))
        elif topics[0] == WETH_WITHDRAWAL and len(topics) == 2 and len(words) == 1:
            wraps.append((token, _topic_address(topics[1]), ZERO, str(int(words[0], 16))))
    # Some wrapped-ETH contracts emit both Deposit and Transfer(0x0 -> dst) for one wrap (seen on
    # zkSync tx 0xe21abb35...): the explorer lists it once, so a Deposit only counts when the
    # contract did not also emit the matching mint/burn Transfer.
    for wrap in wraps:
        if wrap in found:
            found.remove(wrap)  # paired with its Transfer: keep a single entry
        found.append(wrap)
    return found


def check_answer(bundle: EvidenceBundle, cfg: AppConfig, tx: dict | None, receipt: dict | None,
                 raw_corpus: str, fee_token_decimals: int | None = None) -> list[Check]:
    """All checks for one answer. `tx`/`receipt` come straight from the node; `raw_corpus` is every
    response body the tool received while answering (for the invented-address check).
    `fee_token_decimals`: decimals of the node's `feeCurrency` (Celo), resolved by the caller."""
    return [
        _check_status(bundle, receipt),
        _check_value(bundle, cfg, tx),
        _check_fee(bundle, cfg, tx, receipt, fee_token_decimals),
        _check_token_transfers(bundle, cfg, receipt),
        _check_no_double_native(bundle),
        _check_addresses_exist(bundle, cfg, raw_corpus),
    ]


def _check_status(bundle: EvidenceBundle, receipt: dict | None) -> Check:
    if not receipt or receipt.get("status") not in ("0x1", "0x0"):
        return Check("status", "skip", "no receipt status from the node")
    node = "success" if receipt["status"] == "0x1" else "failed"
    if bundle.status == node:
        return Check("status", "pass")
    return Check("status", "fail", f"tool says {bundle.status!r}, node says {node!r}")


def _overview(bundle: EvidenceBundle):
    return next((e for e in bundle.items if e.kind == "overview"), None)


def _check_value(bundle: EvidenceBundle, cfg: AppConfig, tx: dict | None) -> Check:
    overview = _overview(bundle)
    stated = overview.data.get("value") if overview else None
    if tx is None or not isinstance(stated, str) or stated == "unknown":
        return Check("value", "skip", "no value stated or no node transaction")
    node = _int(tx.get("value"))
    tool = _raw_units(stated, cfg.network.native_decimals)
    if node is None or tool is None:
        return Check("value", "skip", "value not comparable")
    return Check("value", "pass") if node == tool else Check("value", "fail", f"tool {tool}, node {node}")


def _check_fee(bundle: EvidenceBundle, cfg: AppConfig, tx: dict | None, receipt: dict | None,
               fee_token_decimals: int | None = None) -> Check:
    fee_fact = next((e for e in bundle.items if e.kind == "fee"), None)
    if fee_fact is None or receipt is None or tx is None:
        return Check("fee", "skip", "no fee fact or no node receipt")
    token = fee_fact.data.get("token")
    if token != cfg.network.native_symbol:
        # Fee paid in a token (Celo CIP-64): the node prices gas in that token, so
        # gasUsed * effectiveGasPrice is the fee in the token's (or adapter's) units.
        if not tx.get("feeCurrency") or fee_token_decimals is None:
            return Check("fee", "skip", f"fee paid in {token}: fee currency or its decimals unknown")
        expected = (_int(receipt.get("gasUsed")) or 0) * (_int(receipt.get("effectiveGasPrice")) or 0)
        stated = _raw_units(str(fee_fact.data.get("fee")), fee_token_decimals)
        return Check("fee", "pass") if stated == expected else \
            Check("fee", "fail", f"tool {stated}, node gasUsed*effectiveGasPrice {expected} (in {token} units)")
    if any(_int(receipt.get(k)) for k in ("operatorFeeScalar", "operatorFeeConstant")):
        return Check("fee", "skip", "operator fee present: formula depends on the network upgrade")
    gas_used = _int(receipt.get("gasUsed"))
    price = _int(receipt.get("effectiveGasPrice"))
    price = price if price is not None else _int(tx.get("gasPrice"))
    if gas_used is None or price is None:
        return Check("fee", "skip", "node receipt lacks gas data")
    expected = gas_used * price + (_int(receipt.get("l1Fee")) or 0)
    blob_gas, blob_price = _int(receipt.get("blobGasUsed")), _int(receipt.get("blobGasPrice"))
    if blob_gas and blob_price:
        expected += blob_gas * blob_price
    stated = _raw_units(str(fee_fact.data.get("fee")), cfg.network.native_decimals)
    if stated == expected:
        return Check("fee", "pass")
    return Check("fee", "fail", f"tool {stated}, node gasUsed*price(+l1/blob) {expected}")


def _check_token_transfers(bundle: EvidenceBundle, cfg: AppConfig, receipt: dict | None) -> Check:
    if receipt is None:
        return Check("token_transfers", "skip", "no node receipt")
    if any(g.what == "Token transfers" for g in bundle.gaps):
        return Check("token_transfers", "skip", "the tool declared its token-transfer list incomplete")
    if any(g.what in ("Explorer transaction data", "Explorer index") for g in bundle.gaps):
        return Check("token_transfers", "skip", "the explorer was unavailable or behind, and the tool declared it")
    native = (cfg.network.native_token_contract or "").lower()
    node = sorted(receipt_transfers(receipt, {native} if native else set()))
    tool = []
    for e in bundle.items:
        if e.kind != "token_transfer":
            continue
        d = e.data
        amount_or_id = (f"{d.get('value')}x{d.get('token_id')}" if d.get("token_id") is not None and d.get("value") is not None
                        else str(d.get("token_id")) if d.get("token_id") is not None else str(d.get("value")))
        tool.append(((d.get("token") or "").lower(), (d.get("from") or "").lower(), (d.get("to") or "").lower(),
                     amount_or_id))
    tool.sort()
    if node == tool:
        return Check("token_transfers", "pass")
    missing = list((Counter(node) - Counter(tool)).elements())  # counts repeats, not just presence
    extra = list((Counter(tool) - Counter(node)).elements())
    return Check("token_transfers", "fail", f"in node logs only: {missing[:3]}; in tool only: {extra[:3]}")


def _check_no_double_native(bundle: EvidenceBundle) -> Check:
    stated = {(str(e.data.get("from")).lower(), str(e.data.get("to")).lower(), str(e.data.get("value")))
              for e in bundle.items if e.kind == "native_transfer"}
    doubles = [e.id for e in bundle.items if e.kind == "internal_call" and e.data.get("moves_value")
               and not e.data.get("same_as") and any(
                   s[2] == e.data.get("value") for s in stated)]
    return Check("no_double_native", "fail", f"internal calls repeating a native movement: {doubles}") if doubles \
        else Check("no_double_native", "pass")


def _check_addresses_exist(bundle: EvidenceBundle, cfg: AppConfig, raw_corpus: str) -> Check:
    corpus = raw_corpus.lower()
    known = {a.lower() for a in list(cfg.address_labels) + list(cfg.native_contracts) + list(cfg.fee_tokens)}
    invented = sorted({a for e in bundle.items for a in ADDRESS_RE.findall(e.text)
                       if a.lower() not in corpus and a.lower() not in known})
    return Check("addresses_exist", "fail", f"addresses not in any source: {invented[:5]}") if invented \
        else Check("addresses_exist", "pass")
