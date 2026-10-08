"""Chain-type profiles: what each kind of EVM network adds on top of the common standard.

The config names the network's type with `network.chain_type`, using Blockscout's own
CHAIN_TYPE values (the operator of an explorer already knows theirs). A profile turns the
type-specific parts of an explorer transaction into typed objects: how the fee was paid,
and extra facts such as Optimism withdrawals or zkSync's status on L1.

Unknown or profile-less types fall back to the generic profile, and the bundle declares
what it could not interpret. Field shapes were checked on live explorers (DECISIONS D20).
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from anychain.collectors.http import CollectorError
from anychain.collectors.types import to_int as _int

# Every CHAIN_TYPE value Blockscout accepts (blockscout/config/config_helper.exs).
BLOCKSCOUT_CHAIN_TYPES = frozenset({
    "default", "arbitrum", "arc", "blackfort", "eden", "ethereum", "filecoin", "optimism", "rsk",
    "scroll", "shibarium", "stability", "suave", "zetachain", "zilliqa", "zksync", "neon", "optimism-celo",
})


# Transaction fields every Blockscout v2 reports, whatever the chain type
# (taken from a recorded eth.blockscout.com response, fixture eth_usdc_transfer).
COMMON_FIELDS = frozenset({
    "authorization_list", "base_fee_per_gas", "block_number", "confirmation_duration", "confirmations",
    "created_contract", "decoded_input", "exchange_rate", "fee", "fhe_operations_count", "from", "gas_limit",
    "gas_price", "gas_used", "has_error_in_internal_transactions", "hash", "historic_exchange_rate",
    "is_pending_update", "max_fee_per_gas", "max_priority_fee_per_gas", "method", "nonce", "position",
    "priority_fee", "raw_input", "result", "revert_reason", "status", "timestamp", "to", "token_transfers",
    "token_transfers_overflow", "transaction_burnt_fee", "transaction_tag", "transaction_types", "type", "value",
})


@dataclass(frozen=True)
class TokenRef:
    """A token named by the explorer (used when a fee is not paid in the native currency)."""

    address: str | None
    symbol: str  # the explorer's symbol, else its name, else the token address
    decimals: int | None  # None when the explorer does not report them: amounts stay raw


@dataclass(frozen=True)
class FeePart:
    label: str  # e.g. "L2 execution", "L1 data", "blob data"
    raw: int  # smallest units of `token`, or of the native currency when token is None
    token: TokenRef | None = None


@dataclass(frozen=True)
class Fee:
    """The fee a transaction paid, split into the parts the network charges."""

    parts: list[FeePart]
    note: str | None = None  # e.g. "paid in USD₮ instead of the native currency"
    warnings: list[str] = field(default_factory=list)  # what makes this fee incomplete; become gaps

    @property
    def single_token(self) -> TokenRef | None:
        tokens = {p.token for p in self.parts}
        return tokens.pop() if len(tokens) == 1 else None

    @property
    def total_raw(self) -> int:
        return sum(p.raw for p in self.parts)


@dataclass(frozen=True)
class ChainFact:
    """One type-specific fact; the bundle numbers it and attaches its source (explorer, or the node
    method in `rpc_method` for facts from node_facts)."""

    kind: str
    text: str
    data: dict = field(default_factory=dict)
    rpc_method: str | None = None


@dataclass
class ChainGap:
    """Something node_facts could not establish (e.g. the explorer and the node disagree)."""

    what: str
    why: str
    needed: str
    cause: str  # a models.GapCause


def classified_as(tx: dict, kind: str) -> bool:
    """Blockscout's own classification of the transaction (field `transaction_types`)."""
    kinds = tx.get("transaction_types")
    return isinstance(kinds, list) and kind in kinds


class ChainProfile:
    """The generic EVM: only what every Blockscout-compatible explorer reports."""

    chain_type = "default"
    owned_fields: frozenset[str] = frozenset()  # top-level tx fields this profile interprets

    def fee(self, tx: dict) -> Fee | None:
        total = _int((tx.get("fee") or {}).get("value"))
        return Fee([FeePart("fee", total)]) if total is not None else None

    def facts(self, tx: dict) -> list[ChainFact]:
        return []

    def generic_failure(self, result: str | None) -> tuple[str, str] | None:
        """(note, source url) when this network type's failure text carries no reason; None otherwise."""
        return None

    def node_facts(self, tx_hash: str, tx: dict,
                   call: Callable[[str, list], object]) -> list[ChainFact | ChainGap]:
        """Facts the explorer may report late, confirmed with the network's own node (`call` = JSON-RPC).

        A CollectorError raised here becomes a gap; nothing is asserted without the node's answer.
        """
        return []


class EthereumProfile(ChainProfile):
    """Ethereum L1 (Blockscout CHAIN_TYPE=ethereum): adds blob transactions (EIP-4844, type 3)."""

    chain_type = "ethereum"
    owned_fields = frozenset({"blob_gas_used", "burnt_blob_fee", "max_fee_per_blob_gas",
                              "blob_versioned_hashes", "blob_gas_price"})

    def fee(self, tx: dict) -> Fee | None:
        base = super().fee(tx)
        blob = _int(tx.get("burnt_blob_fee"))
        if base is not None and not blob and tx.get("blob_versioned_hashes"):
            return Fee(base.parts, warnings=["the transaction carries blobs, but the explorer reports no blob fee; "
                                             "the fee shown may be the execution part only"])
        if base is None or not blob:
            return base
        # Verified live: Blockscout's `fee` is the execution fee only; the blob fee is charged on top.
        return Fee([FeePart("execution", base.total_raw), FeePart("blob data, burnt", blob)])

    def facts(self, tx: dict) -> list[ChainFact]:
        hashes = tx.get("blob_versioned_hashes") or []
        if not hashes:
            return []
        return [ChainFact("chain", f"Carried {len(hashes)} blob(s) of data (EIP-4844), using "
                          f"{tx.get('blob_gas_used')} blob gas.", {"blobs": len(hashes)})]


class OptimismProfile(ChainProfile):
    """OP Stack L2 (CHAIN_TYPE=optimism): L1 data fee, L1->L2 deposits, L2->L1 withdrawals."""

    chain_type = "optimism"
    owned_fields = frozenset({"l1_fee", "l1_fee_scalar", "l1_gas_price", "l1_gas_used",
                              "da_footprint_gas_scalar", "op_withdrawals", "op_interop_messages", "operator_fee"})
    DEPOSIT_TX_TYPE = 126

    def fee(self, tx: dict) -> Fee | None:
        """Blockscout's total is execution + L1 data fee + operator fee (fee_calc, since Isthmus)."""
        base = super().fee(tx)
        if base is None:
            return None
        l1, operator = _int(tx.get("l1_fee")) or 0, _int(tx.get("operator_fee")) or 0
        if not l1 and not operator:
            return base
        execution = base.total_raw - l1 - operator
        if execution < 0:
            return Fee(base.parts, warnings=[f"the explorer's L1 fee ({l1}) and operator fee ({operator}) exceed "
                                             f"its total fee ({base.total_raw}), so the fee is not split"])
        parts = [FeePart("L2 execution", execution)]
        parts += [FeePart("L1 data", l1)] if l1 else []
        parts += [FeePart("operator fee", operator)] if operator else []
        return Fee(parts)

    def facts(self, tx: dict) -> list[ChainFact]:
        found = []
        if classified_as(tx, "op_stack_l1_attributes_transaction"):
            found.append(ChainFact("chain", "L1 attributes transaction: the system transaction the sequencer puts "
                                   "first in every block to record L1 data (OP Stack type 126, classified by the "
                                   "explorer). Not a user deposit.", {"deposit_type": "l1_attributes"}))
        elif _int(tx.get("type")) == self.DEPOSIT_TX_TYPE:
            found.append(ChainFact("chain", "Deposit transaction (OP Stack type 126): created from L1, not signed "
                                   "on L2; its gas was paid on L1. Usually a user deposit through the bridge; "
                                   "network-upgrade deposits use the same type.", {"deposit_type": "deposit"}))
        if classified_as(tx, "op_stack_post_exec_transaction"):
            found.append(ChainFact("chain", "The explorer classifies this as an OP Stack post-execution transaction "
                                   "(type 0x7D); its contents are not interpreted by this tool.",
                                   {"post_exec": True}))
        messages = tx.get("op_interop_messages")
        if isinstance(messages, list) and messages:
            found.append(ChainFact("chain", f"The explorer reports {len(messages)} cross-chain interop message(s) "
                                   "for this transaction; their contents are not interpreted by this tool.",
                                   {"interop_messages": len(messages)}))
        for w in tx.get("op_withdrawals") or []:
            if not isinstance(w, dict):
                continue
            text = f"Started a withdrawal to L1 (nonce {w.get('nonce')}): the explorer reports status {w.get('status')!r}"
            if w.get("l1_transaction_hash"):
                text += f", finalized on L1 in transaction {w['l1_transaction_hash']}"
            found.append(ChainFact("chain", text + ".", {"withdrawal_status": w.get("status"),
                                                         "l1_transaction_hash": w.get("l1_transaction_hash")}))
        return found


class CeloProfile(OptimismProfile):
    """Celo (CHAIN_TYPE=optimism-celo): an OP Stack L2 whose fees can be paid in a token."""

    chain_type = "optimism-celo"
    owned_fields = OptimismProfile.owned_fields | {"celo"}

    def fee(self, tx: dict) -> Fee | None:
        token = ((tx.get("celo") or {}).get("gas_token")) if isinstance(tx.get("celo"), dict) else None
        total = _int((tx.get("fee") or {}).get("value"))
        if not isinstance(token, dict) or total is None:
            return super().fee(tx)
        address = token.get("address_hash")
        ref = TokenRef(address, token.get("symbol") or token.get("name") or address or "an unnamed token",
                       _int(token.get("decimals")))
        return Fee([FeePart("fee", total, ref)], note="as a Celo fee currency")


class ZkSyncProfile(ChainProfile):
    """zkSync Era (CHAIN_TYPE=zksync): a zkEVM rollup; adds the transaction's status on L1."""

    chain_type = "zksync"
    owned_fields = frozenset({"zksync"})
    PRIORITY_TX_TYPE = 255  # L1 -> L2

    def facts(self, tx: dict) -> list[ChainFact]:
        found = []
        if _int(tx.get("type")) == self.PRIORITY_TX_TYPE:
            found.append(ChainFact("chain", "Priority transaction (zkSync type 255): submitted through L1 and "
                                   "executed on L2.", {"priority": True}))
        info = tx.get("zksync")
        if not isinstance(info, dict) or not info.get("status"):
            return found
        steps = [(label, info.get(f"{key}_transaction_hash")) for label, key in
                 (("committed", "commit"), ("proven", "prove"), ("executed", "execute"))]
        done = ", ".join(f"{label} in L1 transaction {h}" for label, h in steps if h)
        text = f"Status on L1, as reported by the explorer: {info['status']!r}"
        if info.get("batch_number") is not None:
            text += f" (batch {info['batch_number']})"
        # The explorer's L1 status can lag by days (real tx 0xb83b7034...: "Sealed on L2" while the node
        # reported the batch executed on L1), so without its hashes we only say what the explorer lists.
        text += f"; {done}." if done else "; the explorer lists no L1 transaction for it yet."
        hashes = {label: h for label, h in steps if h}
        return found + [ChainFact("chain", text, {"l1_status": info["status"], "batch": info.get("batch_number"),
                                                  **hashes})]

    # zks_getTransactionDetails (zkSync Era JSON-RPC): status plus the L1 commit/prove/execute hashes,
    # field names as answered by mainnet.era.zksync.io on 2026-10-07.
    # ethPrecommitTxHash was null on every mainnet batch checked (518336 to 518348), but it is a step.
    NODE_STEPS = (("precommitted", "ethPrecommitTxHash"), ("committed", "ethCommitTxHash"),
                  ("proven", "ethProveTxHash"), ("executed", "ethExecuteTxHash"))

    # The node stores this same text for every failed transaction: "Bootloader currently doesn't return
    # detailed errors" (zksync-era, core/lib/dal/src/transactions_dal.rs, lines 663-670, read 2026-10-08).
    GENERIC_FAILURE = "Bootloader-based tx failed"
    GENERIC_FAILURE_SOURCE = ("https://github.com/matter-labs/zksync-era/blob/948aeaf5786ebc4e9b7dd947cc08a6a1df9a7e21/"
                              "core/lib/dal/src/transactions_dal.rs#L663-L670")

    def generic_failure(self, result: str | None) -> tuple[str, str] | None:
        if result != self.GENERIC_FAILURE:
            return None
        return (f"{self.GENERIC_FAILURE!r} is the text zkSync's node stores for every failed transaction; it "
                "carries no reason.", self.GENERIC_FAILURE_SOURCE)

    EXPLORER_STEPS = (("committed", "commit_transaction_hash"), ("proven", "prove_transaction_hash"),
                      ("executed", "execute_transaction_hash"))

    def node_facts(self, tx_hash: str, tx: dict,
                   call: Callable[[str, list], object]) -> list[ChainFact | ChainGap]:
        info = tx.get("zksync")
        if not isinstance(info, dict) or not info.get("status") or info.get("execute_transaction_hash"):
            return []  # no explorer claim to confirm, or the explorer already names the L1 execution
        details = call("zks_getTransactionDetails", [tx_hash])
        if not isinstance(details, dict):  # null is the node's answer for a hash it does not know
            raise CollectorError("the node does not know this transaction (zks_getTransactionDetails returned "
                                 f"{'null' if details is None else type(details).__name__})", retryable=False)
        explorer = {label: info[key].lower() for label, key in self.EXPLORER_STEPS if info.get(key)}
        node = {label: details[key].lower() for label, key in self.NODE_STEPS if details.get(key)}
        status = details.get("status")
        source = f"zks_getTransactionDetails {tx_hash}"
        data = {"node_l1_status": status, **node}
        # Disagreement (a step only the explorer has, or a different hash): state neither as true.
        if any(node.get(label) != h for label, h in explorer.items()):
            return [ChainGap("L1 status from the node",
                             f"the explorer and the node disagree on the L1 steps (explorer: "
                             f"{_steps(explorer) or 'none'}; node: {_steps(node) or 'none'})",
                             "Check the batch on L1 directly", "source_behind")]
        if not node:
            return [ChainFact("chain", f"The node reports no L1 transaction for it yet either (node status "
                              f"{status!r}).", data, rpc_method=source)]
        missing = [label for label in node if label not in explorer]
        text = f"The node reports this transaction {_steps(node)} (node status {status!r})"
        text += f"; the explorer does not list the {_and(missing)} step{'s' if len(missing) > 1 else ''} yet." \
            if missing else ", the same L1 steps the explorer lists."
        return [ChainFact("chain", text, data, rpc_method=source)]


def _steps(steps: dict[str, str]) -> str:
    return ", ".join(f"{label} in L1 transaction {h}" for label, h in steps.items())


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


class RskProfile(ChainProfile):
    """Rootstock (CHAIN_TYPE=rsk): no extra fields, but the explorer classifies native-contract transactions."""

    chain_type = "rsk"

    def facts(self, tx: dict) -> list[ChainFact]:
        if classified_as(tx, "rootstock_remasc"):
            return [ChainFact("chain", "The explorer classifies this as Rootstock's REMASC transaction: the "
                              "per-block distribution of mining rewards, a system transaction.", {"remasc": True})]
        if classified_as(tx, "rootstock_bridge"):
            return [ChainFact("chain", "The explorer classifies this as a Rootstock bridge transaction (the "
                              "native BTC peg contract).", {"bridge": True})]
        return []


PROFILES: dict[str, ChainProfile] = {p.chain_type: p for p in (
    ChainProfile(), EthereumProfile(), OptimismProfile(), CeloProfile(), ZkSyncProfile(), RskProfile())}

# Fields any profile interprets, to tell "belongs to another chain type" from "unknown".
# A field shared through inheritance (Celo has Optimism's) is credited to the base type.
ALL_OWNED_FIELDS: dict[str, str] = {}
for _profile in PROFILES.values():
    for _name in sorted(_profile.owned_fields):
        ALL_OWNED_FIELDS.setdefault(_name, _profile.chain_type)


def profile_for(chain_type: str) -> tuple[ChainProfile, bool]:
    """The profile for a chain type, and whether it is a dedicated one (False = generic fallback)."""
    profile = PROFILES.get(chain_type)
    return (profile, True) if profile else (PROFILES["default"], False)


def present(tx: dict, name: str) -> bool:
    """A field counts as reported only when it carries a value."""
    value = tx.get(name)
    return value not in (None, "", [], {}, "0", 0)


@dataclass
class FieldAudit:
    """What the explorer reported beyond what the active profile interprets."""

    other_types: dict[str, list[str]]  # chain type -> its fields found in this payload
    unknown: list[str]  # structured fields no profile knows


def audit_fields(tx: dict, profile: ChainProfile) -> FieldAudit:
    """Compare the payload with the configured chain type, to catch a wrong `chain_type`
    and to declare network-specific data nobody interprets."""
    other: dict[str, list[str]] = {}
    unknown: list[str] = []
    for name in sorted(tx):
        if name in COMMON_FIELDS or name in profile.owned_fields or not present(tx, name):
            continue
        if name in ALL_OWNED_FIELDS:
            other.setdefault(ALL_OWNED_FIELDS[name], []).append(name)
        elif isinstance(tx[name], (dict, list)):
            unknown.append(name)  # new plain fields appear across Blockscout versions; blocks are chain data
    return FieldAudit(other, unknown)
