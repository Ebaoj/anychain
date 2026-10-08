# PHASE2: failures and degradation (diagnosis, eth_call, repo grounding, ABI cascade, validator)

- **Status:** DRAFT (waiting for Joabe's approval)
- **Level:** full
- **Opened:** 2026-10-08
- **Project:** anychain (case 1.4, CloudWalk)
- **Type:** feature

## 1. Summary for the decision

- **What is missing:** the tool tells *that* a transaction failed and repeats the explorer's reason. It does not tell *why* in plain words, does not check the chain state behind the failure, and does not use the configured contract repositories at all.
- **What changes for the user:** a failed transaction gets a diagnosis (likely causes, each tied to a fact, and next steps). Calls are explained from the contract's source in the configured repo, with a permalink. Contracts the explorer cannot decode are decoded from the repo, or named as a candidate from a signature database, clearly marked as a guess. The written answer is checked against the facts before it is shown.
- **What does not change:** facts stay sourced; nothing is asserted without proof (a guess is labelled as one); config-only network changes; the tool stays read-only.
- **Risk:** a diagnosis is the place where a false fact is most tempting. Every cause must be stated as "likely" with the fact that supports it, and confirmed by a state read when one exists.
- **Decisions for Joabe:** section 12.

## 2. Context

- Phase 1 (closed 2026-10-07, docs/ACCEPTANCE.md) builds the evidence bundle from the explorer and the node: `src/anychain/bundle.py`.
- The writer (D27) runs on Claude Code and is given only the facts: `src/anychain/writer.py`.
- The config already validates `repos`, `abi_strategy.order` (`explorer, repo_artifacts, repo_source_signatures, signature_db`) and `signature_db`, but only `explorer` is used (`src/anychain/config.py:137`, `src/anychain/bundle.py:630`).

## 3. Problem (verified facts)

| # | Fact | Where | Consequence | Evidence |
|---|---|---|---|---|
| P1 | The revert reason comes only from the explorer; without it, a gap asks for a trace node | `bundle.py:458-474` | 8 of 116 failed sample transactions have no reason text | acceptance results, `status == failed`, revert facts |
| P2 | A known reason is repeated, not explained: "Bootloader-based tx failed" on 40 zkSync failures; `Error(string)` on 53, of which 30 are one Rootstock oracle ("Blocknumber does not match the last publication block"), and the rest mostly short bot codes ("NOT_YET" 8, "!:a2" 3, "AR34"), plus "ERC20: transfer amount exceeds balance" (3), "paused" (1), "block number deadline" (1) | `bundle.py:_add_revert` | the user still has to work out what went wrong | acceptance results, revert facts, counted 2026-10-08 |
| P3 | No `eth_call` read exists; `RpcClient.call` is used only for transaction, receipt, code and chain id | `collectors/rpc.py:24` | allowance, balance or paused state behind a failure are never checked | grep |
| P4 | `repos` are validated but never read; no permalink to source exists | `config.py:186-193` | the case's "repo grounding" task is not done | grep |
| P5 | ABI order lists four strategies; only the explorer runs | `bundle.py:630` | unverified contracts stay undecoded | 781 "Call decoding" gaps in the sample |
| P6 | The architecture's "validator" has no call site; the LLM answer is printed unchecked | `cli.py` | a number or address the model invents would reach the user | D27 review |
| P7 | Facts carry no confidence level; a guess and a confirmed fact look the same | `models.py` | needed once guesses (signature database) exist | model |

## 4. Objective

A failed or hard-to-read transaction gets a sourced diagnosis and decoding from every configured source, with each fact's confidence visible, and no written answer reaches the user unchecked.

## 5. Requirements

- **R1.** WHEN a transaction failed, THE SYSTEM SHALL state a diagnosis: the failing call and reason, one or more *likely* causes each citing the facts that support it, and next steps. **Accept:** a real failed transaction from the sample with `Error(string)` "ERC20: transfer amount exceeds balance" (Celo and Optimism, 3 cases) yields a likely cause "the sender's token balance was lower than the amount" citing the revert fact and the balance read (R3). A short opaque reason ("NOT_YET", "!:a2", "AR34": bot contracts in the sample) yields "the contract's own reason; its meaning is in the contract's source" plus, when a repo or verified source has it, the line that raises it.
- **R2.** IF the explorer reports no revert reason, THEN THE SYSTEM SHALL try to reproduce the call with `eth_call` at the parent block and report what that replay returned *as a replay*, with its limits (other transactions earlier in the same block are not replayed). **Accept:** one of the 8 sample failures without reason gets either a decoded replay reason or a gap saying the node could not replay it (no archive state).
- **R3.** WHEN a likely cause depends on chain state that a standard read can check (ERC-20 allowance and balance, `paused()`, a deadline against the block time), THE SYSTEM SHALL read it with `eth_call` at the parent block and state the value with its block. **Accept:** the balance in R1 is read and shown with block number and source; the "paused" case reads `paused()`.
- **R4.** WHEN a configured repo contains the called contract (matched by verified source hash, or by address in config), THE SYSTEM SHALL cite the function's source with a permalink to the pinned commit and lines. **Accept:** a BRLC transaction on Ethereum (proxy 0xAC17…) cites `cloudwallk/brlc-token@74a5498` file and lines of the called function.
- **R5.** THE SYSTEM SHALL decode calls, events and custom errors through the configured ABI order: explorer, repo artifacts, signatures computed from repo source, signature database. A signature-database match SHALL be stated as a candidate ("selector matches `transfer(address,uint256)` in 4byte; not confirmed"), never as the decoded call.
- **R6.** THE SYSTEM SHALL mark every fact's confidence: `confirmed` (from the source of record, or two sources agree), `single_source` (one source, not cross-checked), `candidate` (inferred, e.g. a signature match). The rendered output and the LLM payload SHALL show it.
- **R7.** WHEN the LLM returns an answer, THE SYSTEM SHALL check it before showing it: every `[E#]` exists; every address, hash and number with 4+ digits appears in the evidence; IF a check fails, THEN it SHALL retry once with the list of problems, and IF it fails again, show the evidence-only output with a note.
- **R8.** IF any source is missing or fails (explorer, RPC, repo clone, signature database, LLM, time budget), THEN THE SYSTEM SHALL still answer with what it has and say what is missing and what would fix it (existing gap rules), and the event log SHALL record the cause.

Each R has at least one eval case from a real recorded transaction (section 13).

## 6. Non-functional

- **Latency:** state reads and repo lookups run inside the 30 s budget and in parallel where possible (D26 pattern). Repo clones are cached on disk (`storage.cache_dir`) and pinned to a commit; a cold clone happens once.
- **Cost:** the validator retry adds at most one LLM call per answer.
- **Privacy:** unchanged; RPC/API URLs never reach the model.
- **Observability:** new gap causes reuse D24; validator failures logged as events.

## 7. Threats

- **Untrusted text:** revert strings, contract names, NatSpec comments and repo files are written by third parties. They go to the model as data inside the evidence, never as instructions; the system prompt says so; the validator (R7) blocks invented values.
- **Signature database poisoning:** 4byte entries are user-submitted; collisions exist. R5 keeps them as candidates only.
- **Repo trust:** the BRLC repo is an unofficial mirror (approved for the demo on 2026-10-08); citations name the repo and commit, so the reader sees the source.

## 8. Out of scope

No `git push`, no deploy. No tracing node (`debug_traceTransaction`) as a requirement: used only if `rpc.supports_debug_trace` is true. No compiling Solidity (signatures are parsed from source, not compiled). Security notes and gas suggestions are Phase 4.

## 9. Approach

- `collectors/rpc.py`: `eth_call(to, data, block, from_, value)` returning raw bytes or revert data.
- `diagnosis.py` (new): a small rule table, one class per known failure pattern (allowance, balance, paused, deadline, slippage-style `Error(string)`, out of gas, zkSync bootloader failure), each declaring which facts trigger it and which reads confirm it.
- `collectors/repo.py` (new): shallow clone at the pinned commit into the cache, index `.sol` files (contract, function, event, error, line ranges) and artifact JSONs (Hardhat `artifacts/`, Foundry `out/`); selector to source location; permalink builder.
- `decoder.py`: ABI cascade per `abi_strategy.order`; `signature_db` client with a cache.
- `models.py`: `confidence` on `Evidence`; `render.py` and `writer.evidence_payload` show it.
- `validator.py` (new): checks of R7; `cli.py` wires validate, retry, fallback.

## 10. Tasks (each ends in a commit, with tests written first from real recordings)

1. T1 (R7): validator, wired into `explain`. Smallest and protects everything after it.
2. T2 (R6): confidence on facts, rendered and sent to the model.
3. T3 (R3): `eth_call` reads, with typed results and block-pinned sources.
4. T4 (R1, R3): diagnosis rules for the failures seen in the sample (balance, paused, deadline, opaque contract codes, out of gas, zkSync bootloader), each confirmed by a read when possible; allowance added for the common ERC-20 case even though the sample has none (tested on a recorded real case found for it).
5. T5 (R2): revert replay at the parent block for failures without a reason.
6. T6 (R4, R5): repo clone, index and permalinks; repo artifacts and source signatures in the ABI cascade.
7. T7 (R5): signature database as a candidate source.
8. T8 (R8): degradation matrix, one eval case per missing source.
9. T9: architecture docs (both), decisions, clean-context review per task, Phase 2 report.

## 11. Rollout and reversal

Local tool, no deploy. Each source can be turned off by config (`abi_strategy.order`, `signature_db.enabled`, `repos: []`), which returns to Phase 1 behaviour.

## 12. Open decisions (Joabe)

- **D1. Where this spec lives.** The global rule sends tickets to Jira (KAN), and asks before creating one for a project without a Jira space. Proposed default: keep it in the repo (`docs/specs/`), as the rest of this project's decisions.
- **D2. Order.** Proposed default: T1 to T9 as listed (validator first).
- **D3. Signature database (4byte) calls.** It is an external public service that sees which selectors we look up (public data only). Proposed default: on, cached, candidates only.

## 13. Tests and tracing

| Requirement | Test (written before the code) | Evidence |
|---|---|---|
| R1 | real failed tx per pattern, expected likely cause and cited facts | pasted test run |
| R2 | a sample failure without reason, replay result or gap | pasted |
| R3 | balance and `paused()` reads pinned to the parent block, from recordings | pasted |
| R4 | BRLC call cites file, lines and commit | pasted |
| R5 | unverified contract decoded from repo; 4byte match stated as candidate | pasted |
| R6 | every fact has a confidence; a candidate never reads as confirmed | pasted |
| R7 | invented number or `[E99]` rejected, retry, then fallback | pasted |
| R8 | one case per missing source | pasted |

## 14. How to verify

`uv run pytest -q` (offline, real recordings) and live `anychain explain` on the eval transactions. Phase 2 is done when the failure and degradation cases pass (spec section 9 of the original case plan).

## 15. Definition of done

- [ ] D1 to D3 decided.
- [ ] T1 to T9 committed, each with its tests and a clean-context review.
- [ ] Architecture docs (en and pt-BR) updated; Phase 2 explained to Joabe in plain Portuguese.
