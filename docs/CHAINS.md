# Chain types: what each network adds, and what AnyChain does with it

Research done on 2026-10-07, before changing code, so support is driven by the protocols and
by Blockscout's source rather than by the transactions we happened to sample.

**Two kinds of source, used for different questions:**
- **What the tool receives**: Blockscout's source, `blockscout/blockscout`, mainly
  `apps/block_scout_web/lib/block_scout_web/views/api/v2/transaction_view.ex` (the per-chain-type
  hook `do_with_chain_type_fields` and the classifier `transaction_types`), the per-chain views
  (`optimism_view.ex`, `celo_view.ex`, `zksync_view.ex`, `ethereum_view.ex`) and the fee formula in
  `apps/explorer/lib/explorer/chain/transaction.ex` (`fee_calc`).
- **What it means**: each network's official docs or contracts source (linked per section).
  Where docs were silent, the chain's own contract source or a live read decided it.

Status legend for the last column of each table: **done** (interpreted), **declared** (shown as
"not interpreted"), **missing** (ignored today: a gap in this tool).

---

## Common to every Blockscout (any chain type)

| Field / concept | Meaning | AnyChain |
|---|---|---|
| `fee.value` | Blockscout's total fee. `fee_calc`: **l2 fee + l1 fee + operator fee** (the last two are zero outside OP chains) | done |
| `transaction_types` | Blockscout's own classification of the tx: `coin_transfer`, `contract_call`, `contract_creation`, `token_transfer`, `token_creation`, `blob_transaction`, `set_code_transaction` (EIP-7702), `op_stack_l1_attributes_transaction`, `op_stack_post_exec_transaction`, `rootstock_bridge`, `rootstock_remasc`, `sponsored_transaction` | **missing**: it already tells us what we guess from other fields |
| `authorization_list` | EIP-7702 authorizations | done (D17, D18) |
| `revert_reason` | decoded object, `{"raw": ...}`, or text | done (D13, D20) |

## Ethereum (`ethereum`)

Sources: `ethereum_view.ex`; EIP-4844, EIP-7702.

| Item | Detail | AnyChain |
|---|---|---|
| Blob transactions (type 3) | `blob_gas_used`, `blob_gas_price`, `max_fee_per_blob_gas`, `blob_versioned_hashes`, `burnt_blob_fee` = blob gas used × blob gas price. **Not included in `fee.value`** | done (D20) |
| EIP-7702 (type 4) | delegation set or cleared before execution; last valid one per account wins | done |

## OP Stack (`optimism`)

Sources: `optimism_view.ex`; [OP fees](https://docs.optimism.io/stack/transactions/fees);
[deposits spec](https://specs.optimism.io/protocol/deposits.html); `Predeploys.sol`.

| Item | Detail | AnyChain |
|---|---|---|
| Fee parts | **execution** (`gas_used × (base + priority)`), **L1 data fee** (`l1_fee`), **operator fee** (since Isthmus: `operatorFeeConstant + gasUsed × operatorFeeScalar × 100`; Blockscout adds `operator_fee` only when > 0). All three are in `fee.value` | **partial**: L1 split done; the operator fee is folded into "L2 execution" today (wrong label when present) |
| Deposit type 126 (0x7E) | not signed on L2, gas prepaid on L1, no refund; **deposits pay no operator fee** | done (hedged wording) |
| L1 attributes tx | the first tx of every block, from `0xdead…0001` to `0x42…15`, source-hash domain 1. Blockscout flags it as `op_stack_l1_attributes_transaction` | **missing**: we could say exactly "sequencer system transaction" instead of "either a deposit or a system tx" |
| User deposits | from L1 `TransactionDeposited` events, sender possibly aliased, domain 0, `mint` = ETH brought from L1 | partial (same hedged wording) |
| Upgrade deposits | domain 2 (network upgrades); domains 3 and 4 for interop | declared only through the hedged wording |
| Type 0x7D | `op_stack_post_exec_transaction` (new Blockscout classification) | **missing** |
| Withdrawals | `op_withdrawals[]`: nonce, status (e.g. "Ready to prove"), `l1_transaction_hash` only when finalized | done |
| Interop messages | `op_interop_messages` (cross-chain messages) | **missing**: our profile listed a non-existent `op_interop` field |

## Celo (`optimism-celo`): an OP Stack L2

Sources: `celo_view.ex`; [fee abstraction](https://docs.celo.org/developer/fee-abstraction);
[fee currencies](https://docs.celo.org/build/tools/contracts/fee-currencies); live reads on forno.celo.org.

| Item | Detail | AnyChain |
|---|---|---|
| Everything from OP Stack | Celo's identity in Blockscout is `{:optimism, :celo}` | done (inherits) |
| Fee currency (CIP-64, type 123 / 0x7B) | `celo.gas_token`: the fee is paid in that token, debited before and credited after execution (`debitGasFees`/`creditGasFees`) | done (D20) |
| Adapters for 6-decimal tokens | USDC, USDT, USA₮, XAUt0 pay through an **adapter** address; the fee is in **adapter units**. The USDT adapter `0x0E2A…` reports `decimals() = 18`; the USDC adapter `0x2F25…` has **no `decimals()`** (call reverts), which is why the explorer sends `decimals: null` | done: raw units + gap (D20) |
| Fee-currency allowlist | official list maps each adapter to its token (e.g. `0x2F25…` → USDC `0xcebA…`, 6 decimals) | **missing**: naming the real token would turn "raw units of 0x2F25…" into a readable fee |
| CELO is also an ERC-20 | token `0x471E…` (Celo native asset, ERC-20). **A native CELO send also appears as a CELO token transfer** (verified live: tx 0x64f5270e…, same amount) | **missing**: the movement is shown twice |
| Fee flows | token transfers to/from the zero address and to `FeeHandler` (`0xcD43…`) and `SequencerFeeVault` (`0x42…11`) | partial: labels only (D20) |

## zkSync Era (`zksync`): a zkEVM rollup

Sources: `zksync_view.ex`; [tx lifecycle](https://docs.zksync.io/zksync-protocol/era-vm/transactions/transaction-lifecycle);
`matter-labs/era-contracts`: `Constants.sol`, `L2BaseToken.sol`.

| Item | Detail | AnyChain |
|---|---|---|
| L1 status | `zksync.status` is one of: Processed on L2, Sealed on L2, **Sent to L1**, Validated on L1, Executed on L1; plus batch number and commit/prove/execute L1 hashes | done (quoted as reported) |
| Native ETH is a system contract | `L2BaseToken` (`0x…800a`) emits `Transfer` for **every** ETH movement, including the fee prepay to the bootloader and the refunds ("might be removed later on", per its source). Blockscout shows them as token transfers | **missing**: ETH shown twice; fee and refunds look like unrelated transfers |
| Fees | bootloader (`0x…8001`) takes a prepay and refunds the unused part; the net is the fee | **missing**: derivable from those transfers, not said |
| Paymasters | type 113 (0x71, EIP-712) can name a paymaster that pays the fee; Blockscout exposes no paymaster field | **missing**: detectable when the prepay to the bootloader comes from an address other than the sender |
| Type 255 (0xFF) | priority (L1→L2) transactions | **missing** |
| System contracts | `0x8001`… `0x8012` | done: labels in config (D20) |

## Rootstock (`rsk`)

Sources: `transaction_view.ex` classifier; `rsksmart/rskj` `PrecompiledContracts.java`.

| Item | Detail | AnyChain |
|---|---|---|
| Native contracts | Bridge `0x…01000006` (BTC peg), REMASC `0x…01000008` (block rewards, a system tx from the zero address in every block). Blockscout flags them as `rootstock_bridge` / `rootstock_remasc` | partial: labels + zero-address rule (D20); the classification is not read |
| Native contracts have no code or ABI | asking for "a verified contract" for them can never be satisfied | **missing** (review r9 finding 9) |
| Tx types | legacy only (type 0) in the samples | done |

## Gnosis (`default`)

| Item | Detail | AnyChain |
|---|---|---|
| Native currency XDAI | a stablecoin bridged from Ethereum; fees in XDAI | done |
| Chain-type fields | none on the explorer (generic Blockscout) | done |
| Explorer moved | `gnosis.blockscout.com` → `gnosisscan.io` (HTTP 301, same Blockscout v12) | done (D20) |

---

## Gaps found, by priority

Ordered by the harm of the current behaviour: a wrong or double-counted fact first, then a
vaguer answer than the data allows, then missing context.

1. **Native currency shown twice** (zkSync ETH via `0x…800a`, Celo CELO via `0x471E…`): declare a
   network's native-token contract in config; transfers of it are the record of the native
   movement (and, on zkSync, of the fee prepay and refunds), not a second asset.
2. **OP operator fee mislabelled** as part of "L2 execution": add the `operator_fee` part.
3. **Blockscout's `transaction_types` is ignored**: use it to state exactly what is now hedged
   (OP L1 attributes vs user deposit, Rootstock bridge/REMASC, type 0x7D) and to cross-check our
   own inference.
4. **Celo adapter fees in raw units**: map adapter → token from the official allowlist (config
   data, not code), so the fee reads "0.0078 USDC".
5. **zkSync fee payer**: when the bootloader prepay comes from another address, say a paymaster
   paid the fee; state the net fee as prepay minus refunds.
6. **Native contracts** (Rootstock): mark them in config so the tool says "native contract, no
   source code exists" instead of asking for a verified contract.
7. **Fields not yet declared**: `op_interop_messages` (fix the wrong `op_interop` name), type 0x7D,
   zkSync type 255.
