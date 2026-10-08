# Roadmap

Agreed items for later phases, so they are not lost between sessions. Each item says why it matters and what "done" looks like.

## Phase 4

### Private devnet that mirrors the CloudWalk setup
- **Why:** no public network reproduces CloudWalk's situation: a private node, a private Blockscout and its own contracts. This is the strongest proof that retargeting needs config only.
- **What:** on the `home server` server (Docker), run a local EVM node (Anvil) plus a self-hosted Blockscout. Deploy the BRLC contracts from the configured repo, generate real transactions (transfers, approvals, a paused-token revert, an access-control revert, an out-of-gas), then point AnyChain at it with a new YAML only.
- **Done when:** `anychain explain` and `anychain eval` run against the devnet with a `configs/devnet.yaml`, no code changes, and the README shows one sample conversation from it.
- **Risk:** Blockscout needs Postgres; `home server` has 4 GB RAM. Check memory before starting; fall back to the backend without the frontend.
- **Decided:** 2026-10-07, by Joabe.

## Open questions
- Anthropic API key for the LLM writer: own key (`ANYCHAIN_ANTHROPIC_API_KEY`) vs the Aulai key.
- Using the unofficial mirror `cloudwallk/brlc-token` in the demo, with a README note.

## Backlog: level C findings (decided by Joabe on 2026-10-08)

True but imprecise answers, found by the reviews and the acceptance run (docs/ACCEPTANCE.md). Joabe chose to fix C3, C11 and C12 now (D26) and keep these for later. Each item names the real transaction or file it came from.

| # | Finding | Where it was seen |
|---|---|---|
| C1 | When the explorer marks a token as scam and hides its transfers, say so (today the transfer is simply absent, matching the explorer) | Gnosis tx 0x19c135c2… ("! Pepe", explorer reputation "scam") |
| C2 | A token whose symbol equals the native currency's ("Wrapped XDAI" listed as "XDAI") should be named as a token, not the currency | Gnosis tx 0x310bc26b…, token 0xe91D… |
| C4 | zkSync Mint/Withdrawal events and the fee flow of type 255 (priority) transactions are declared, not interpreted | earlier review rounds, fixture zksync_priority_l1 |
| C5 | OP Stack: the sender of an L1 deposit is an aliased address; label it as such | earlier review rounds |
| C6 | Fee flow edge cases: refund only, negative net, missing sender | earlier review rounds |
| C7 | Config hygiene: misplaced YAML comments (Celo, zkSync), `fee_tokens` overriding explorer decimals without a warning, no hex validation of config addresses | earlier review rounds |
| C8 | Celo fee paid in a token: the debit and credit transfers (to GAS_FEE_ADDRESS 0x…0Ce106A5, the fee handler, the refund) read as ordinary payments; group them under the fee | Celo tx 0x1c282ade…, 0xc435ce27… |
| C9 | Celo: the same token shown as "USD₮" in the fee and "USDT" in the transfers | same txs as C8 |
| C10 | WETH unwrap/wrap listed as transfers to and from the zero address; say unwrap/wrap | Ethereum tx 0xd58d0906… |
| C13 | Rootstock: the same address in two casings (EIP-1191 from the explorer, EIP-55 in decoded arguments) in one answer | Rootstock tx 0x3a59f01a…, 0xee197943… |
| C14 | Data sent to an account without code: an archive `eth_getCode` at the block could prove it had no code then (today: "cannot confirm") | Ethereum tx 0x6692980f… |
| C15 | Rootstock native contracts: the published ABIs (`@rsksmart/rsk-precompiled-abis`) as a configured repo source | Rootstock tx 0xf6009eb2… |
| C16 | zkSync node status "failed" is quoted as is; a reader may take it for an L1 failure (unconfirmed what it means; check the zkSync source first) | review of D25 |
