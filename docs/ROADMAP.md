# Roadmap

Agreed items for later phases, so they are not lost between sessions. Each item says why it matters and what "done" looks like.

## Phase 4

### Private devnet that mirrors the CloudWalk setup
- **Why:** no public network reproduces CloudWalk's situation: a private node, a private Blockscout and its own contracts. This is the strongest proof that retargeting needs config only.
- **What:** on a Linux server (Docker), run a local EVM node (Anvil) plus a self-hosted Blockscout. Deploy the BRLC contracts from the configured repo, generate real transactions (transfers, approvals, a paused-token revert, an access-control revert, an out-of-gas), then point AnyChain at it with a new YAML only.
- **Done when:** `anychain explain` and `anychain eval` run against the devnet with a `configs/devnet.yaml`, no code changes, and the README shows one sample conversation from it.
- **Risk:** Blockscout needs Postgres; the server has 4 GB RAM. Check memory before starting; fall back to the backend without the frontend.
- **Decided:** 2026-10-07, by the author.

### Dockerfile
- **Why:** the original plan (section 11) welcomes a simple Dockerfile "if it costs little", and the case asks for "lightweight and deployable, runs locally".
- **What:** one image that runs `anychain` and the API with a config mounted from outside; no secrets inside. Built and run on a server, never on the mac (CLAUDE.md).
- **Done when:** the README quickstart, tested from a clean clone, also works with `docker run`.
- **Decided:** 2026-10-08, by the author (moved here from Phase 3's gaps).

## Open questions
- ~~Anthropic API key for the LLM writer~~ Resolved 2026-10-08: the writer runs on Claude Code (D27); a key is needed only for a deployed service.
- ~~Using the unofficial mirror `cloudwallk/brlc-token` in the demo~~ Approved by the author on 2026-10-08, with a README note that it is an unofficial mirror (pinned commit 74a5498).
- ~~Re-run of the 1,800-transaction acceptance after D26~~ Done at the end of Phase 2 (D37). Until it runs, the measured rates in docs/acceptance-report.md describe the code before D26 (C11 and C12 changed internal-call facts on every network; D26 re-checked them on the 40 recordings and by review, not on the sample).

## Backlog: level C findings (decided by the author on 2026-10-08)

True but imprecise answers, found by the reviews and the acceptance run (docs/ACCEPTANCE.md). The author chose to fix C3, C11 and C12 now (D26) and keep these for later. Each item names the real transaction or file it came from.

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

Added at the close of Phase 2 (2026-10-08), sent to the backlog by the author:

| # | Finding | Where it was seen |
|---|---|---|
| C17 | 0.8 % of answers came out incomplete while explorers were slow (time budget used up, declared as gaps): a longer budget, shorter per-request timeouts, or fewer retries on the main request | Phase 2 acceptance run (D37) |
| C18 | The allowance rule has no real recorded case (only a fake reader in tests) | T4 review (D31) |
| C19 | A proven event signature does not name the contract or log it belongs to (topics are looked up once per answer) | T7 review (D35) |
| C20 | Pinning an implementation address in `address_map` has no effect on calls to its proxy, and nothing warns | T6 part 2 review (D34) |
| C21 | Claude Code adds the logged-in user's email to the model's context whatever the flags | D27 review |
| C22 | A base function overridden by a public state variable (`uint public override v`) is still named as the function writing a reason: the index does not read state variables | review of D38 |
| C23 | Shown code line numbers count as known numbers for the answer check, so a 4-digit amount equal to one passes | review of D38 |
| C25 | A sensitive-name note on a function that does nothing (Compound `mintVerify`: the `mint` prefix of the name rule) | review of D39, sent to the backlog by the author 2026-10-08 |
| C26 | A function authorized by a signature (`ecrecover(...) == owner`) gets "no check of the caller": literally true, may mislead | review of D39, sent to the backlog by the author 2026-10-08 |
| C27 | A selfdestruct anywhere in the contract's chain repeats the same note on every function (1inch router: 45 functions) | review of D39, sent to the backlog by the author 2026-10-08 |
| C24 | Uniswap V3 SwapRouter (0xE592…1564) gets no code fact: its verified source holds `Multicall.sol` and `PeripheryPayments.sol` twice under different paths, and the index gives up on duplicate names (no false fact, a missing one) | review of D39 |
