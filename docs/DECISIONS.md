# Decisions

Each entry: decision, alternatives, reason, trade-off. Entries are history: when a later decision replaces an earlier one, the earlier text is struck through and points to the replacement.

## D1. Second network is Optimism, not Base
- **Alternatives:** Base (spec's first suggestion), Sepolia.
- **Reason:** `base.blockscout.com` answers API calls with a Cloudflare 403 challenge (checked 2026-10-07, also with a browser-like User-Agent). Optimism's Blockscout (`explorer.optimism.io`) answers normally.
- **Trade-off:** none for the portability proof; Base can be re-added later as one more YAML file.

## D2. Optimism RPC is `mainnet.optimism.io`
- **Alternatives:** `optimism-rpc.publicnode.com` (same provider as the Ethereum config).
- **Reason:** publicnode refuses receipts for non-recent blocks on Optimism without a personal token ("Archive requests require a personal token").
- **Trade-off:** a different provider per network, which is exactly what config-only retargeting allows.

## D3. Decode with our own decoder, not Blockscout's pre-decoded fields
- **Alternatives:** use `decoded_input` / `decoded` returned by Blockscout.
- **Reason:** the ABI cascade (repo artifacts, source signatures, 4byte) needs a decoder we control. Using the same decoder for every source keeps one code path and lets each decoding report where its ABI came from.
- **Trade-off:** one extra request per contract (`/smart-contracts/{address}`), cached per run.

## D4. Proxies: merge proxy ABI with the implementation ABI
- **Reason:** USDC and most upgradeable tokens are proxies; the proxy's own ABI does not contain `transfer`. Blockscout lists `implementations`, which we follow.
- **Trade-off:** if two ABIs define the same selector, the implementation wins (it is added last).

## D5. Evidence is built without any LLM; the LLM only writes
- **Reason:** spec principle 2. Facts are numbered (`E1`, `E2`...) and each has a source: explorer page, explorer API endpoint, or JSON-RPC method + params. The LLM receives only this list.
- **Trade-off:** the LLM cannot add useful context from its own knowledge. That is intended.

## D6. Read-only `staticcall`s are grouped into one fact
- **Reason:** a single Uniswap swap produced 7 staticcalls (balance reads) that buried the real value movements.
- **Trade-off:** the individual reads are not listed; the explorer link still shows them.

## D7. Pending transactions stop early
- **Reason:** Blockscout returns mempool transactions with a "fee" that has not been paid. We now report "pending, nothing is final" plus a gap, instead of a misleading fee.

## D8. Tests replay recorded real traffic (custom httpx transport)
- **Alternatives:** respx mocks written by hand.
- **Reason:** `scripts/record_fixture.py` records every real response a run needs; `ReplayTransport` serves them back, and can simulate an outage of any host. No test data is invented, and tests run offline.
- **Trade-off:** fixtures are large (~300 KB each) because Blockscout responses are verbose.

## D9. No agent framework
- **Reason:** spec section 2. A plain call to the Anthropic SDK is easier to read and debug. Tool use for chat comes in phase 3, as a simple loop.

## D10. CloudWalk network and the BRLC contracts repo
- **Finding (2026-10-07):** the official `github.com/cloudwalk` org has no public Solidity repo today, but the BRLC token contracts were public there until ~May 2024. A full copy lives at `github.com/cloudwallk/brlc-token`: commits by CloudWalk engineers (`@cloudwalk.io`), PR numbers from the original repo, last commit 2024-05-06. The copying org is unverified, was created 2024-01-03, and pushed all 17 repos on 2024-05-09, so it is a mirror, not an official account.
- The repo's `.openzeppelin/` manifests show deployments on chain ids 2008 and 2009, which `chainid.network` lists as CloudWalk Testnet and CloudWalk Mainnet (currency CWN). Their explorers are not publicly reachable (mainnet redirects to cloudwalk.io; testnet resolves to a private 10.x IP). This matches the case's wording: "CloudWalk private".
- The same contracts are also on **Ethereum mainnet**: BRLC proxy `0xAC176d9e75384F7d71275bb9D5265281CC0Dd284`, verified, implementation `BRLCTokenBridgeable` (`0xbEA441d7cf3f79b57cc5ae33251a084A063825dc`), with real transactions from 2023 (transfer, approve, setPauser, transferOwnership).
- **Decision:** `cloudwalk.example.yaml` now has the real chain id, currency and repo (pinned to commit `74a5498`), with only the internal URLs left as placeholders. The mirror is used because it is the only public source of this code; the pin prevents silent changes by an unknown owner.
- **Trade-off:** we depend on an unofficial host. If it disappears, the repo-grounding step degrades and says so.

## D11. Retry inside the request, a time budget, and no queue (for now)
- **Decision:** network trouble (timeout, dropped connection, 429, 5xx, a 200 with HTML) is retried up to 3 attempts, with 0.5s and 1s pauses, for both the explorer and the RPC. A problem with the request itself (404, other 4xx, most JSON-RPC errors) is not retried. All requests of one explanation share a time budget (`assistant.time_budget_s`, 30s): when it runs out, we answer with what we have.
- **Every gap says whether it is `retryable`**, so the reader knows if "try again in a few minutes" can help.
- **Alternatives:** a job queue (Redis/SQS + worker) for retries.
- **Reason:** the user is waiting for an interactive answer. Short failures are solved by retrying now; requests that are wrong fail again however often they are retried. Long outages are where a queue helps, but only when there is somewhere to deliver the completed answer later (a support ticket, a notification). A local CLI has no such place.
- **Trade-off / production path:** in production, analyses whose gaps are all `retryable` would go to a queue and the ticket would be updated when the source comes back. The `retryable` flag is the piece that makes that possible.
- Real case: Blockscout took 22.7s to answer `/internal-transactions` for a 2020 transaction (fixture `eth_contract_creation`); the answer keeps every other fact and marks internal calls as retryable.

## D12. "Unverified" is decided by the missing `abi`, not by HTTP 404
- Verified live: for an unverified contract, `/smart-contracts/{address}` answers **200** with bytecode and no `abi`; 404 is for unknown addresses. Only a missing ABI means "not verified". Timeouts and 5xx on this endpoint are reported as an "ABI lookup" gap (retryable), never as "not verified".
- Proxy implementations listed in the transaction payload are also tried, so an unverified proxy whose implementation is verified still decodes.

## D13. Revert reasons arrive decoded
- Verified live: Blockscout's `revert_reason` is an object such as `{"method_call": "Error(string reason)", "parameters": [...]}`, or `{"raw": "0x..."}`. It is formatted as text; the raw object is kept in the evidence data.

## D14. Malformed payloads are tested by editing real recordings
- Live explorers rarely send broken data, so `tests/test_malformed.py` takes a real recorded response and changes one field (null topics, null value, list instead of object, HTML instead of JSON). Each step of the bundle runs inside `_safely()`: one broken part becomes a gap, the rest of the answer survives. The CLI has a last-resort handler, so the user never sees a stack trace.

## D15. Values are formatted by ABI type
- A 42-character string starting with `0x` used to be treated as an address. Now only values whose ABI type is `address` are checksummed; strings and bytes are shown exactly as decoded.

## D16. Status edge cases found in the second review
- **Dropped/replaced:** Blockscout stores these with `status: "error"` and `result: "dropped/replaced"` (Blockscout source, `transaction.ex`). We check `result` first, so a dropped transaction is reported as "never executed", not "failed (reverted)".
- **Failure reason in `result`:** when `revert_reason` is empty but `result` carries text such as "out of gas", that text is reported as the failure reason.
- **Pre-Byzantium receipts** (before block 4,370,000) have no `status` field. They are reported as "outcome not recorded", never as "failed", and the cross-check is skipped.
- **Explorer behind the RPC:** if the explorer still says pending but the node already has a receipt, the answer uses the receipt and declares the explorer lag as a retryable gap.
- **Retries are not stacked:** network trouble is retried inside `request_json` (3 attempts). The RPC client retries only the node's own "busy" answers (JSON-RPC -32005 / 429), so a network outage costs 3 attempts, not 9.
- **The time budget is soft:** each attempt's timeout is capped by the time left, so the total can overshoot only by one slow read.
- **Recorded failures:** the recorder now stores real timeouts, so `eth_contract_creation` replays the actual `/internal-transactions` timeout instead of a missing record.
- **Third review:** an explorer 404 for a tx the node already mined is index lag (retryable), not "unknown hash"; "dropped" on the explorer but mined on the node is flagged and the receipt wins; when the explorer answers but lags, the RPC path still decodes the call with the explorer's ABI; a lookup that failed for a non-network reason is not retryable anywhere; `eth_chainId` accepts hex or plain numbers; a non-object receipt is a gap, not "no receipt".

## D17. Transaction types found by a bug hunt on real mainnet data
A bug hunt with varied real transactions found facts that were well formatted, sourced, and wrong. Each fix has a real recorded fixture.
- **Internal value only moves on `call` and `create`/`create2`.** `delegatecall` and `callcode` run another contract's code inside the caller, so their `value` is the caller's own context and nothing moves (Lido `submit`, `eth_lido_submit`, used to show a phantom ETH transfer to the Lido implementation). `callcode` is grouped with `delegatecall` on purpose: in the EVM its value goes from the caller to itself. A failed `call` with value moves nothing. Each internal fact carries `moves_value` (true / false / null when not known).
- **`selfdestruct` is not interpreted yet:** no real recording exists, so its value is reported neutrally ("the explorer records a value of X") instead of guessing who received it.
- **EIP-7702 (type 4):** each `authorization_list` entry becomes a fact ("delegation set" / "cleared" for address 0x0), with its status when not `ok`. A type-4 tx with no data and no value is described as a delegation change, not a "plain transfer". Limitation: we state the delegation; we do not explain what the delegated code did.
- ~~**Data sent to an account with no code** is not a function call and needs no ABI. We rely on the explorer's `is_contract`.~~ **Superseded by D18:** `is_contract` is today's state, so this is no longer asserted.
- **Internal `create`** names the deployed contract from `created_contract`.
- **Anonymous events** (no signature topic, e.g. MakerDAO `LogNote`) are decoded only when exactly one anonymous event of the ABI fits the log; otherwise the gap says so, instead of claiming the ABI is missing.
- **RPC sources** now show the exact call (method + params), and the LLM receives each fact's sources, with the rule that it may repeat but never invent links.
- **Review of D17 (fifth review):** the explorer's `is_contract` is today's state, so a 7702 account that delegated, acted and later revoked looked like "no code" (real tx 0xf5199e66…). Now:
  - ~~"Did the target run code in this tx?" answered by a precompile range (0x…01 to 0x…ff) and the node's `eth_getCode` just before the block.~~ **Superseded by D18:** both rules produced false facts in real cases. What remains from this round: a valid delegation set or cleared by this very tx is trusted. (Also learned here: public nodes refuse old state; a block ~1.5h old already needed "a personal token".)
  - A delegation set in the same tx is used as an ABI source, so the call still decodes (`execute(bytes32,bytes)` on 0xf5199e66…).
  - Authorizations are stated as applied only when the explorer marks them `ok`; `invalid_*` ones say "was not applied", and a missing status says "validity not reported" (real tx 0x26118f7f…).
  - A type-4 tx with no data is described by what it carries ("N authorizations"), not by a claim that it "only changes delegation".
  - Anonymous events must consume the log data exactly (a trailing unpadded `bytes` is allowed), so a log with leftover data is not forced into an anonymous event.
  - Only explorer page URLs are sent to the LLM; API and RPC URLs may be internal on a private network. RPC sources keep the method and params as `detail`.
  - An internal call with value whose success is not reported is "attached, outcome unknown" (`moves_value: null`), not a transfer.

## D18. We do not assert "this account had no code" (sixth review)
- **Decision:** "data sent to an account without code is not a function call" is asserted only when this very transaction cleared the account's EIP-7702 delegation (valid authorizations are applied before execution; the last valid one per account wins). In every other case we state what is known ("the explorer lists it as having no code today") and declare a non-retryable gap asking for an execution trace.
- **Alternatives tried:** reading the code with `eth_getCode` just before the block, plus a precompile range. Rejected after review: code can appear earlier in the same block (another tx's delegation, a contract created and destroyed in the block), and precompiles differ per chain and fork (OP's P256 at 0x…0100 executes with no stored code: real tx 0x0032185d…, fixture `op_p256_precompile`). Each rule produced a false fact in some real case.
- **Trade-off:** fewer assertive answers. The definitive answer needs an execution trace, which phase 2 adds when `supports_debug_trace` is on.
- **RPC-only path:** with no explorer flag, today's code from the node decides "contract or not" (code today = contract), the same criterion as the explorer; no code today gives the same honest "unknown".
- **Known limitation (not fixed):** the mirror case. An account that delegated *after* a transaction is listed by the explorer today as a contract (`proxy_type: eip7702`), so an older data transaction to it is decoded against today's delegate. Only a trace or historical code can settle it.
- **Several authorizations for one account:** only the last valid one is stated as in effect; earlier valid ones are "replaced by a later authorization".
- **No endpoint addresses reach the LLM:** gap texts sent to the model have URLs and the RPC host replaced by "[endpoint]"; the user still sees them in the evidence list.
- **Anonymous events:** decoded in lenient mode so an unpadded trailing `bytes` is accepted, then the exact-reuse check rejects any leftover data.

## D19. Seventh review (Fable model), before the phase 1 commit
- **Anonymous wording only when it can apply:** a log is described as "matched none of the anonymous events" only if its first topic is not a known signature and some anonymous event has that many indexed fields. A normal event missing from the ABI keeps its topic0 and the "no ABI matches" gap (real DeFi Saver tx 0x92f208d3…, where the DSProxy ABI has an anonymous `LogNote` but the logs are `ActionEvent`s from delegatecalled actions).
- **EIP-7702 delegate field:** read from `address_hash` or, on older Blockscout versions, `address`. A missing delegate is "target not reported", never "cleared".
- **RPC-only path:** a failed `eth_getCode` keeps the selector fact and adds a retryable "Contract check" gap; only "no code today" leads to the "cannot confirm" wording.
- **Undecoded call on a delegated account:** the gap names the delegate whose code ran (real OP tx 0x86aec918…), not the account, which can never be "verified".
- **JSON consistency:** superseded authorizations have `applied: false, superseded: true`; an internal `create` without a success flag is "not reported", like `call`.
- **Known limitation:** an explorer can return zero internal transactions while other facts prove some ran (seen on explorer.optimism.io for 0x86aec918…). We never state "no internal calls", but cannot tell "none" from "not indexed".
