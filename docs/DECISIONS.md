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

## D20. Chain-type profiles, named after Blockscout's CHAIN_TYPE
- **Why:** two networks (Ethereum, Optimism) hid assumptions. Six were tested, chosen for what each could break. Three false or incomplete facts appeared:
  - **Celo:** the fee was shown as "0.0036 CELO"; it was paid in USD₮ (Celo fee currency, `celo.gas_token`).
  - **Ethereum blob txs (type 3):** Blockscout's `fee` is the execution fee only; the blob fee (`burnt_blob_fee`) is charged on top, so the shown fee was ~10% low on the real tx 0x77215ad1….
  - **zkSync:** fee payments and refunds through the bootloader (0x…8001) appeared as unexplained ETH transfers, and the tx's L1 status (`zksync.status`, batch, commit/prove/execute hashes) was ignored.
- **Decision (proposed by Joabe):** the config declares `network.chain_type`, using the exact values Blockscout accepts (`default`, `ethereum`, `optimism`, `optimism-celo`, `rsk`, `zksync`, `arbitrum`, ...; from `blockscout/config/config_helper.exs`). The operator of an explorer already knows this value. Each type has a profile in `chains.py` that turns its payload into typed objects (`Fee` with `FeePart`s and an optional fee `TokenRef`, `ChainFact`s). Celo inherits Optimism, as in Blockscout (`optimism-celo`).
- **Still any EVM:** the default profile only uses fields every Blockscout reports. A chain type without a dedicated profile (e.g. `arbitrum`) works with the default one and says so. Payload fields owned by another type raise a "Chain type" gap ("the config says X, the explorer looks like Y"), catching a wrong config; unknown structured fields are declared as not interpreted.
- **Network knowledge in config, not code:** `address_labels` names special addresses per network (zkSync system contracts, taken from `matter-labs/era-contracts` `Constants.sol`). A test scans `src/` for hardcoded hosts, chain ids, currency symbols and addresses.
- **CloudWalk (Stratus, read in `cloudwalk/stratus`):** a standard, centralized EVM, so the template uses `chain_type: default`. Findings now in the template: `eth_gasPrice` always returns 0 (no fees), `debug_traceTransaction` is served (`supports_debug_trace: true`, used in phase 2), and unidentified clients can be refused, so the RPC URL carries `?app=anychain` (Stratus reads `app`/`client` query params or `x-app` headers).
- **Not done:** no `stratus` profile without seeing what CloudWalk's Blockscout returns; the private devnet in ROADMAP (phase 4) is where that gets checked.
- **Real-world change seen during the work:** gnosis.blockscout.com started redirecting (HTTP 301) to gnosisscan.io, the same Blockscout v12. The live client follows redirects; the test client now does too, and the Gnosis config points at the new host (a config-only change).
- **Eighth review (Fable) of D20, real cases on the new networks:**
  - A Celo fee token whose decimals the explorer does not report (USDC fee adapter `0x2F25…`) was shown as "7822697203928125 token"; now "7822697203928125 raw units of 0x2F25…" plus a "Fee" gap. `TokenRef.decimals` is `None` when unknown.
  - Revert data `{"raw": "0x"}` (Gnosis, real tx 0xf354bf88…) is stated as "reverted without any revert data", not "undecoded revert data 0x"; `{"raw": null}` means nothing was reported.
  - OP Stack type 126 is described as "deposit-type: not signed by an L2 account; either a deposit from L1 or a sequencer system transaction". The fixture `op_deposit` turned out to be the sequencer's L1 attributes transaction (from the depositor account `0xDeaD…0001`), not a user deposit; its old test asserted a false text.
  - A transaction sent from the zero address is a system transaction (Rootstock's REMASC reward call at the end of every block), never a "plain transfer".
  - System addresses are named in config: OP Stack predeploys and depositor (Optimism, Celo), Rootstock Bridge and REMASC, with their upstream sources in comments.
  - An unknown `chain_type` value no longer stops the config from loading: the generic profile is used and a "Chain type" gap says so (older Blockscout versions used values such as `celo`).
  - Fee fallbacks are no longer silent: blobs without a reported blob fee, or an L1 fee larger than the total, raise a "Fee" gap.
  - **Known limitation:** plain (non-structured) fields of chain types we have no profile for are not detected (e.g. a hypothetical `zkevm_*` scalar); only structured blocks are declared. New plain fields appear in every Blockscout release, so flagging them would be noise.

## D21. Explorer and RPC answers become typed objects at the edge
- **Why (Joabe):** after D20, chain-specific logic lived in classes, but `bundle.py` still read raw dicts: 110 `.get(...)` calls, `to` read in 10 places. A renamed Blockscout field (EIP-7702 `address_hash` vs `address`) had to be handled in several places, and every reader had its own guards.
- **Decision:** `collectors/types.py` parses each answer once into frozen dataclasses: `AddressRef`, `Token`, `Authorization`, `RevertReason`, `Transaction`, `TokenTransfer`, `InternalCall`, `Log`, `RpcTransaction`, `RpcReceipt`. The explorer and RPC clients return these objects. Parsing never raises on an odd field: it becomes `None` (unknown). Field-name variations of transaction data live here (one exception: `explorer.py` reads the proxy-implementation field of contract metadata).
- **Alternatives:** pydantic models (rejected: a strict validation error would cost the whole object, while we want field-level tolerance); keep dicts (rejected: the reason for this change).
- **Safety net:** `tests/test_golden.py` freezes the full answer of all 31 recorded transactions. The refactor had to keep every one identical, word for word, and did. Regenerating golden answers is a deliberate step (`ANYCHAIN_UPDATE_GOLDEN=1`) whose diff is reviewed.
- **Kept raw on purpose:** a few evidence `data` values (block, timestamp, gas as the explorer sent them, the original revert object) stay exactly as received, so golden answers did not change. Chain profiles still read `tx.raw`: each profile is the edge parser for its own chain-specific block.
- **Result:** `bundle.py` raw reads went from 110 to 10, and those 10 are the program's own dicts or the deliberately raw values.
- **Ninth review (Fable), with a differential harness:** 13,151 mutated-fixture cases run through the pre-refactor and the new code. All 31 real recordings were identical, most differences were stricter parsing (improvements), and these regressions were found and fixed:
  - A missing address was filled with the text "(unknown address)" and then used as a real address, even in HTTP requests. `AddressRef.address` is now `None` when unreadable; a call without a readable target and a log without a readable emitter become gaps.
  - Unreadable fields silently took a default meaning: non-text or missing `raw_input` was read as "no call data" and stated as a plain transfer. Now `raw_input`/`input` are `None` when absent or not text (Blockscout and JSON-RPC always send them, "0x" when empty) and become a gap; a non-list `authorization_list` becomes a gap.
  - `{"method_call": ""}` re-opened the D20 fix ("undecoded revert data 0x"): an empty name now means "not decoded".
  - Empty `{}` RPC answers are rejected as broken payloads instead of being read as a mined transaction.
  - "Called f() on X with ." on calls and events without arguments (every OP Stack L1 attributes tx): the only golden answer that changed, on purpose.
  - The golden test approved a missing golden file by writing it; it now fails unless regeneration is requested.
  - `tests/test_types.py` tests the parsers directly with the odd shapes the harness found. Dead fields and a duplicate int parser were removed.
  - Rerun of the same harness after the fixes: zero invented addresses, zero placeholder requests, zero crashes.

## D22. The seven gaps from the chain research (docs/CHAINS.md)
- **Native currency counted once:** `network.native_token_contract` names the contract that also records native movements as token transfers (zkSync `L2BaseToken` 0x…800a, which emits `Transfer` for every ETH move per its source; Celo's CELO token 0x471E…, verified on a live native send). Its transfers become "native movement" facts, never a second asset.
- **Fee flow:** with `network.fee_collector` (zkSync's bootloader), prepay minus refunds is stated as the net fee and **checked against the explorer's fee** (a mismatch raises a gap). When the prepay comes from someone other than the sender, the fee payer is named as a paymaster (real tx 0x092a7ba3…).
- **OP operator fee:** Blockscout's total is execution + L1 data + operator fee (`fee_calc`); the operator fee is now its own part. Not seen live (zero on OP Mainnet and Celo today), tested on a real recording with the field added.
- **Blockscout's classification (`transaction_types`) is read:** the OP L1 attributes transaction is stated exactly (no longer "either a deposit or a system tx"); a user deposit is stated as a deposit; OP type 0x7D and Rootstock bridge/REMASC are named from the explorer's own classification.
- **Celo fee adapters:** `fee_tokens` in config maps each adapter to a symbol and the decimals of its fee units. Each value was read on-chain: `expectedDecimals()` = 18 for the standard Celo adapters, `adapterDecimals()` = 18 for Circle's `FiatTokenFeeAdapter` (USDC), which has no `decimals()` and is why the explorer sends `decimals: null`. Without a config entry the fee stays in raw units with a gap.
- **Native contracts:** `native_contracts` in config (Rootstock Bridge and REMASC). Calls and events of those contracts say "built into the node, no source code or ABI" instead of asking for a verified contract.
- **Declared, not interpreted:** `op_interop_messages` (the profile had listed a non-existent `op_interop`), OP type 0x7D, zkSync priority transactions (type 255).
- Network knowledge stays in YAML (addresses, symbols, decimals with their on-chain sources in comments); the code only knows the concepts.

## D23. Tenth review fixes and the acceptance criterion
- **Native value counted once across all fact kinds:** an internal call carrying the same (from, to, value) as a native movement already stated (Celo CELO token transfers synthesized from internal sends; zkSync fee prepay and refunds) now says "the same movement as E#" and carries `same_as` in its data (real Celo tx 0x6be4971d…).
- **zkSync user calls visible:** `network.system_address_max` (0xffff, `MAX_SYSTEM_CONTRACT_ADDRESS` in era-contracts) groups calls between two system contracts into one line; before, the 30-call limit hid every user-level internal call on zkSync, which also made a test pass for the wrong reason.
- **Native contracts have published ABIs:** "no source code or ABI" was false (the Rootstock Bridge's ABI is in rskj). Native contracts now go through the normal ABI lookup; when none is found the gap asks for the published ABI in a configured repo. The RPC-only path also recognises native contracts.
- **Paymaster wording:** "the ETH fee was prepaid by X (a paymaster)", plus what the sender paid the paymaster in tokens in the same tx, net of returns (85.78 NODL on the real fixture).
- **Acceptance criterion (docs/ACCEPTANCE.md):** levels A/B must have zero false facts, measured on 300 random transactions per network checked against the node; level C findings go to Joabe. Rules from real mistakes are in the project `CLAUDE.md`, enforced by hooks that run the suite after every edit and block a commit unless it passes.

## D24. Event log: every answer is recorded, so problems can be found and counted
- **Why (Joabe):** a gap protects the user from a false fact, but only the person reading that answer learned something broke. In production nobody would be told.
- **Every gap has a cause** (`models.GapCause`): `source_unavailable` (timeout, 5xx, rate limit), `source_error` (a source refused or sent something unreadable), `source_behind` (explorer not indexed yet, or disagreeing with the node), `processing_error` (our code failed on the payload, or our arithmetic contradicts a source: always a defect), `config_error` (wrong chain id or chain type), `pending`, `not_interpretable` (no ABI, truncated list, unknown field, hash not found: an expected limit). The first five are problems; the last two are not. `_gap` requires the cause, so every new site has to choose; it is never derived from the gap's text (the one exception, old saved runs, is below).
- **One event per answer** (`events.RunEvent`): network, hash, source (`cli`, later `api`, `canary`), outcome (`ok`, `degraded`, `crash`), duration, every gap with its cause, and, for canary runs, every check against the node. A crash, which the user only sees as "Unexpected error", is logged with its message.
- **Sinks are classes behind one interface** (`EventLog.record`), chosen by `storage.event_sink`. Built: `SqliteEventLog` (local file `storage.sqlite_path`) and `NullEventLog`. Logging never costs the user an answer: a failing sink prints a warning and the answer goes out.
- **Query:** `anychain log` prints, per network, answers, how many had a problem, crashes, average time, and each problem cause with its share; `--problems` lists the recent answers with their causes and failed checks (filters `--network`, `--cause`, `--hours`).
- **The silent kind (a false fact nothing noticed)** is only caught by checking answers against an independent source. The acceptance script writes each checked answer to the same log as `canary` (with the time it ran); `--log-results` loads saved runs once (an answer already logged is skipped). Gaps saved before causes existed are the one place a cause is read from the text, by the same rule, checked against every distinct reason in the 07/10 run; those rows carry the results file's time, as they have no time of their own.
- **Log upkeep:** a re-run or re-check of the same canary answer replaces the earlier one (`record(replace=True)`, keyed by network, source and hash), so counts never double. The acceptance script writes canary answers only for the default results folder, or with `--log`; smoke runs in another folder stay out. `anychain log` opens the file read-only and never creates one.
- **Clean-context review of this change** found, and these were fixed with tests that fail without the fix: a log path that could not be opened crashed `explain` (now it falls back to no logging, with a warning); our own failure on an event log was labelled an expected limit (now `processing_error`, while an unreadable log from the explorer is `source_error`); about 25 gap sites relied on the default cause, several of them configuration or source faults; a failed ABI lookup on a native contract was reported as "no ABI".
- **Production design, not built (this is a case):** a `QueueEventLog` sink publishes the same `RunEvent` (wire format `events.as_json`) to a queue (e.g. SQS). A worker consumes it, writes it to the store, and alerts by rate per network and cause over a window (for example: any `processing_error` or `crash`; `source_unavailable` above 5% in an hour; any failed canary check). A scheduled canary runs the acceptance check on a small daily sample per network. Queueing keeps the answer path fast and the alerting off it; swapping the sink is a config change.
- **First finding from the log:** on Optimism, 25 of 300 answers (8.3%) were incomplete because the explorer's ABI lookup ran out of the 30 s budget (average answer 14 s against 3 to 5 s elsewhere). Not a false fact (the gap says so), but exactly what the alert is for.

## D25. zkSync L1 status: the explorer's view is never asserted, the node confirms it
- **Found by the 30-answer review (level B):** every sampled zkSync answer said "not yet committed to L1". The explorer still reported "Sealed on L2" with no L1 hashes, while the node (`zks_getTransactionDetails`, `zks_getL1BatchDetails`) reported the batches committed, proven and executed on L1 days earlier (real tx 0xb83b7034…, batch 517442, executed 2026-09-19). Rechecking the 300 saved zkSync answers: 297 carried that false sentence. The old test asserted it as expected behaviour.
- **Why the automatic checks missed it:** none of checks 1 to 6 compares L1 status. Check 7 now does (docs/ACCEPTANCE.md).
- **What the tool says now:** the explorer's status is stated as the explorer's ("the explorer lists no L1 transaction for it yet"), with its step hashes in the fact's data. When the explorer names no L1 execution, the profile asks the configured node (`ChainProfile.node_facts`, zkSync only) and compares step by step (precommit, commit, prove, execute):
  - the node has steps the explorer lacks: it states the node's steps and names exactly which ones the explorer does not list yet;
  - both list the same steps: it says so;
  - a step only the explorer has, or a different hash: a `source_behind` gap ("the explorer and the node disagree"), and neither side is stated as true;
  - neither has a step: "The node reports no L1 transaction for it yet either";
  - the node answers null (it does not know the hash): a `source_error` gap.
  The node is only asked after its chain id matched the config; otherwise nothing it says is used.
- **A second review found a false sentence in the first version of this fix** ("the explorer's L1 status is behind" was said without comparing the steps) and that the precommit step (`ethPrecommitTxHash`, null on every mainnet batch checked) was ignored. Both fixed, with tests from the real node answer.
- **Check 7 is independent of the tool's node where it can be:** only the official node answers `zks_*` (zksync.drpc.org returns null, 1rpc 502 on 2026-10-07), and it is the tool's own node. So every step it names is proven on Ethereum through other providers: the L1 transaction succeeded, and the zkSync Era diamond proxy (0x3240…0324) logged that step for this batch (`BlockCommit` and `BlockExecution` with the batch as first topic; `BlocksVerification` covering it as a range; topics computed from the signatures and checked on batch 517442). Times are the L1 block times, so an answer is judged by what existed when it was given. What is not independent: which steps exist at all still comes from the zkSync node (a step that node omits cannot be caught). A precommit step has no checked event yet, so its presence gives no verdict.
- Field names of `zks_getTransactionDetails` (`status`, `ethPrecommitTxHash`, `ethCommitTxHash`, `ethProveTxHash`, `ethExecuteTxHash`) were read from mainnet.era.zksync.io on 2026-10-07; the real case is recorded as fixture `zksync_explorer_l1_behind`.
- **Mixed recording times, on purpose:** the four older zkSync fixtures (`zksync_native_eth_send`, `zksync_paymaster`, `zksync_priority_l1`, `zksync_processed_on_l2`) were recorded before the tool asked the node. A real `zks_getTransactionDetails` answer recorded on 2026-10-07 was added to each, so their goldens pair the explorer of the original recording ("Sealed on L2", no hashes) with the node of 2026-10-07 (committed and proven). Every response is real; the pair was not observed at one moment. It tests the comparison logic, not a historical state.
- **Acceptance after the fix:** the same 300 zkSync transactions, re-run with this code, pass all 7 checks (300/300 on L1 status, 0 tool errors). The other five networks were not re-run: since their run, the only changes that reach them are gap causes and one gap's wording (the Rootstock native-contract lookup), and their golden answers show no fact changed.

## D26. Level C fixes chosen by Joabe (2026-10-08): speed, internal-call cutoff, revert wording
- **C3, answers cut short by the time budget:** on Optimism, 25 of 300 sampled answers hit the 30 s budget and lost ABI lookups. Timing one of them request by request: 16 sequential explorer requests at about 1.5 s each (8 ABI lookups took 12.7 s). The explorer client now fetches each list and each contract's metadata once per explanation (`ExplorerClient._once`, failures kept too), and `BundleBuilder._prefetch` asks in parallel (6 workers) for what the steps will need: the three transaction lists, then the metadata of the call target and of the emitters of the logs the events step decodes, then the implementations those name. The steps then run as before, in the same order. `tests/test_prefetch.py` replays all 40 real recordings with and without the prefetch: same requests, same answers. Live, the same 25 transactions: none ran out of time (0/25, was 25/25), median 30 s to 11 s.
- **C11, the internal-call cutoff dropped calls that moved value:** the first 30 calls were shown. Now every call that transferred the native currency (value above zero, not a delegatecall/callcode whose value is only context) is listed, and the rest fill the room left, in the explorer's order. The gap says how many are shown, and that every call that carried the currency is among them (or, when the explorer's list was cut by paging, that later calls are unknown). Real cases: Ethereum tx 0xd58d0906… (122 internal calls, the 3 value calls were at positions 48, 70 and 95, all hidden) and the DSProxy recipe fixture, where three transfers of 175.49 ETH were hidden.
- **C12, "this internal call failed" for calls undone by a parent:** the explorer's `error` field is now read. "Parent reverted" becomes "undone because a call above it reverted", "Reverted" becomes "this internal call reverted", any other value is quoted. Both values appear in the real recordings (14 "Parent reverted", 1 "Reverted" before this change).
- **Clean-context review of D26** (no false fact found) led to: the prefetch skips the internal-call list while the explorer is still indexing it (the step does not ask then), follows implementations one level only (as `abi_for` does), skips a call target whose delegation this transaction cleared, and sends one request per contract even when the payload spells an address in two casings; `tests/test_prefetch.py` now compares request counts, not just the set. The cutoff claim says "outside the system-contract group" when zkSync system calls (summed, not listed) carried value. A failed internal create uses the same reason wording as calls. Tests added for each.

## D27. The writer runs on Claude Code by default; Anthropic and OpenAI APIs are options
- **Why (Joabe, 2026-10-08):** no API key to manage for local use, the demo and the evaluation suite; "we will use Claude Code". The APIs stay available for a deployed service (Phase 3), where a CLI process per question adds about 2 s and an individual account is not the right way to serve other people.
- **Design:** `writer.py` has one interface (`LlmBackend.complete(system, user)`) and three classes, chosen by `llm.provider`: `claude_code` (default), `anthropic`, `openai`. Same prompt and same evidence payload for all three.
- **Claude Code is isolated:** `claude -p --output-format json` with no tools (`--tools ""`), no user settings (`--setting-sources ""`), no MCP servers (`--strict-mcp-config`), no saved session, run from an empty temporary directory, the evidence on stdin and the prompt as `--system-prompt`. Without tools the model can only write from the facts it is given. Measured on 2026-10-08: about 600 tokens of overhead isolated, against the user's whole environment (USD 1.03 equivalent for a one-word reply) without the flags. Each flag was checked against `claude --help` (2.1.293). The CLI has no temperature or output-length options, so those config fields apply to the API backends only.
- **OpenAI:** Chat Completions over HTTP with `httpx` (no extra dependency); field names from the official openai-python SDK source (`max_completion_tokens`, since `max_tokens` is deprecated). Keys come from the environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`), filled from the vault or the host's secret store (or a local `.env`, which `load_dotenv` reads; never committed).
- **Verified live:** `anychain explain` on Ethereum tx 0xd58d0906… with the default backend, 16 s end to end, answer in pt-BR citing [E#].
- **Clean-context review of D27** found, all fixed with tests: an `ANTHROPIC_API_KEY` in the environment (for example from an old `.env`, which `load_dotenv` reads) silently replaced the claude.ai login, proven live with a fake key (401); the Claude Code child now runs without `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` and `OPENAI_API_KEY`. OpenAI: a null `content` (refusal) became a crash instead of the evidence-only fallback; the error message quoted OpenAI's text, which echoes the key's last characters (now only status and error type); `temperature` can be left out (`null`), as reasoning models reject other values; the HTTP client is closed after each call; the Anthropic client gets `timeout_s`.
- **Known limits:** Claude Code adds the logged-in user's email address to the model's context whatever the flags (seen in the review's live call); the prompt does not ask for it, and the validator (Phase 2) checks addresses, values and hashes, not emails. The OpenAI endpoint is fixed (no `base_url` option for Azure or a proxy). `.env.example` still lists `ANTHROPIC_API_KEY`; it is only read by the anthropic backend now.

## D28. The written answer is checked against the evidence before it is shown (PHASE2 T1, R7)
- **Why:** the model is told to use only the evidence, but nothing checked it; the architecture's validator had no call site (D27 review).
- **What is checked** (`validator.py`), against exactly the text the model was given (`writer.evidence_payload`):
  - every evidence id cited exists, in any form (`[E3]`, `[E3, E9]`, `E9`, `[e9]`, `[E 9]`);
  - every hex value is in the evidence: an address or hash (40 or 64 hex digits) must be a whole evidence value; an abbreviation (prefix…suffix, at least 4 hex digits) must fit one; a slice of 8 to 16 hex digits, not zero-padded, may come from inside a longer value (decoded calldata);
  - every link (with or without scheme, markdown emphasis and `#fragment` ignored) is one of the explorer pages the model was given;
  - every number with 4 or more digits, with decimals, or with a scale word is a number of the evidence: rounding or cutting to the precision written, thousands separators (`,` `.` space), the Portuguese format and scale words with 2 or more significant digits ("5,65 milhões") are accepted; "1 milhão" is too rough to check and is sent back; a dropped minus sign is fine, an added one is not; a date must be the evidence's date. Arithmetic is exact (200-digit precision).
- **Flow** (`writer.write_checked`, used by `explain`): write; check; if problems, write again with the list of problems (each cut to 160 characters, at most 20); check; if still problems, withhold the text and show the evidence only, with a note naming what was wrong. The event log keeps the outcome (`runs.writer`: `ok`, `retried`, `withheld`, `unavailable`, `skipped`; added to old logs on open, read as empty in read-only old logs) and what the model stated outside the evidence (check `answer_check`: `retried` or `fail`). `anychain log` shows retries, withheld and model-less answers per network; `--problems` lists withheld and model-less answers.
- **First real catch:** in a recorded real answer (developer mode, Ethereum tx 0xd58d0906…), the model wrote "PoolManager, ERC-6909": true about Uniswap v4, but not in the evidence. The base prompt now also forbids standards from memory and computed values.
- **Live, 2026-10-08:** support mode passed on the first try; developer mode was fixed on the retry. Duration varied between runs (15 s to 65 s for the same transaction); the bundle took 4.4 s, the rest is the model through Claude Code.
- **Clean-context review of T1** tried adversarial answers; fixed, each with a test written first: zero-padded addresses and long calldata slices passed as hex; citation variants were not checked; space thousands separators, short scale words ("mi", "bi"), one-digit scaled numbers, numbers glued after letters, small decimals, added minus signs, scheme-less links and 2-digit abbreviations passed; bold or fragment links and spaced or dotted numbers were rejected; withheld answers were missing from `--problems` and what the model invented was not stored.
- **Limits:** integers under 4 digits without decimals ("999 USDC") are not checked; scientific notation is not parsed; a wrong statement made only of true values (e.g. swapping sender and receiver) is not caught: that is the evaluation suite's job (Phase 3). Claude Code also gives the model the user's email (D27); the validator does not check emails.
