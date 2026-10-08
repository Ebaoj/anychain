# PHASE3: interface and evaluation (API, web UI, chat with tools, metrics, eval suite)

- **Status:** IN PROGRESS (approved by Joabe on 2026-10-08 with the proposed defaults D1 to D5)
- **Level:** full
- **Opened:** 2026-10-08
- **Project:** anychain (case 1.4, CloudWalk)
- **Type:** feature

## 1. Summary for the decision

- **What is missing:** the tool answers one question per command, on the command line only. There is no conversation, no web page, no API, no way to measure answer quality across a fixed set of cases, and no usage metrics beyond the event log.
- **What changes for the user:** a local web page and API. The user pastes a hash, picks a mode, gets the explanation with clickable sources, a confidence badge per conclusion and a "missing data" panel, then asks follow-up questions in a chat. In the chat the model can ask for more data (read contract state, open a repo file, look at another transaction), and every answer it gets becomes a new citable fact. `anychain eval` runs a fixed set of real cases and reports accuracy, citation coverage and hallucination rate; `anychain metrics` prints usage numbers from SQL. Asking again about the same transaction answers from a local cache (no new explorer or node calls), and a list of hashes can be explained in one batch command that resumes where it stopped.
- **What does not change:** facts stay sourced, the answer check (validator) runs on every chat answer, nothing is asserted without a source, the tool stays read-only, network changes stay config-only.
- **Risk:** a chat where the model chooses what to look up is where it can drift from the evidence; every tool result is a fact with a source, and every chat answer goes through the validator.
- **Decisions for Joabe:** section 12.

## 2. Context

- Phases 1 and 2 closed (D37): `anychain explain` builds a sourced evidence bundle with diagnosis, replay, repo source and signature candidates; the writer runs on Claude Code (D27) and its answer is checked (D28); an event log in SQLite (D24) counts problems per network.
- Phase 2.5 closed (D38 to D43, PHASE2_5.md): the called function's code and heuristic security notes as facts; ABIs from repo artifacts; access-control and deadline-parameter rules; each conclusion labelled CONFIRMED / LIKELY / UNKNOWN (also in the event log); next steps for a non-technical reader and for a developer; the structured answer (`explain --json`, schema `anychain.answer/1`), which this phase's API returns.
- The original case plan, section 5 (interfaces), 7 (agent evaluation), 8 (metrics) and 9 (phase 3 "done when `anychain eval` runs and the UI does the full demo").

## 3. Problem (verified facts)

| # | Fact | Where | Consequence |
|---|---|---|---|
| P1 | Commands are `explain`, `log`, `repos sync`; no `chat`, `eval`, `metrics` | `cli.py` | the original interface list is not met |
| P2 | No web server or page; FastAPI is not a dependency | `pyproject.toml` | the case's "simple interface (web preferred)" is not met |
| P3 | Modes exist (support, developer, auditor: one prompt each) but only on the CLI | `prompts/`, `cli.py` | |
| P4 | The event log has the diagnosis label (D42) but no mode, diagnosis rule, ABI source, tokens, cache hit or user feedback | `events.py` runs table | the metrics the case asks for cannot be computed |
| P6 | Every explain fetches everything again; nothing is kept per (network, hash) | `bundle.py`, `cli.py` | repeated questions (the API will get many) cost explorer and node calls and seconds; public explorers have usage limits |
| P7 | Explaining many hashes exists only inside the acceptance script | `scripts/acceptance.py` | no user command for volume |
| P5 | No eval set: of the 8 categories the plan requires on Ethereum, recorded real cases exist for 5 (ERC-20 transfer, DEX swap, explicit revert reason, unverified contract, node unavailable) plus a passed deadline (D41); none yet for an allowance revert, an out-of-gas failure, a Solidity custom error. An access-control refusal was searched for in 1,600 recent failures on five networks and none was found (D41) | `tests/fixtures/` | the eval cannot cover what the plan asks |

## 4. Objective

A local API and web page with a chat whose every answer is grounded and checked, plus an evaluation command and a metrics command whose numbers come from real cases and SQL.

## 5. Requirements

- **R1. API.** THE SYSTEM SHALL serve `POST /explain` (hash, mode, optional question), `POST /chat` (session id, message) and `GET /health` (active network; status of explorer, node and LLM), locally, with FastAPI. `/explain` returns the structured answer of D43 (`anychain.answer/1`); the optional question works as in R12. **Accept:** `/health` on the Ethereum config reports the network and each source's state from a real probe; `/explain` returns the same answer as `explain --json` for the same hash.
- **R2. Web page.** THE SYSTEM SHALL serve one static page (no frontend build) with: the active network at the top, hash field, mode selector, chat area, clickable sources, a badge per conclusion with its label (CONFIRMED, LIKELY, UNKNOWN; D42), the next steps of the selected mode's reader, security notes marked as heuristics (D39), a "missing data" panel when there are gaps, and thumbs up and down buttons on each answer, saved with that answer's run (R6). **Accept:** the full demo runs in a browser (R9).
- **R3. Chat with tools.** WHEN the user asks a follow-up question, THE SYSTEM SHALL let the model request a tool from a fixed list (read contract state with `eth_call` at a block, show a function's verified code or a file from a configured repo, explain another transaction hash), run it, add each result to the session's evidence as a numbered, sourced fact, and answer citing it. Tools are read-only and limited per turn. **Accept:** on a real failed transaction, "what was the sender's balance before?" makes the model ask for the read, the read becomes a new fact, and the answer cites it.
- **R4. Every chat answer is checked** by the validator (D28) against the session's evidence, with the same retry and withhold rules.
- **R5. Modes** in API and UI (support, developer, auditor), using the existing prompts.
- **R6. Metrics.** THE SYSTEM SHALL record per answer (the diagnosis label is already recorded, D42): mode, diagnosis rule, ABI source of the main call, latency, tokens (when the backend reports them), and optional user feedback (thumbs up or down from the page); `anychain metrics` SHALL print 3 to 4 SQL queries with their SQL (failure causes, diagnosis levels, ABI source share, satisfaction by mode).
- **R7. Eval.** `eval/cases.yaml` SHALL hold at least 8 real Ethereum cases and 2 of a second network, each recorded as a fixture, covering: ERC-20 transfer, DEX swap, explicit revert reason, allowance revert, out of gas, unverified contract, Solidity custom error, node unavailable. Each case states the expected status, diagnosis rule and label, ABI source and entities that must appear; a category with no real case after the search is reported as missing, never filled with made-up data (PHASE2_5 D2). `anychain eval` SHALL run them offline (recorded data) through the whole pipeline including the LLM, and report: status and category accuracy, citation coverage (share of factual sentences citing evidence), hallucination rate (validator problems; target zero), share of degradation declared correctly, latency and tokens per case. The report is saved (`eval/report.md` plus JSON) with its date and commit, so the README's results table (Phase 4) is copied from a real run.
- **R8. Production impact page** (`docs/IMPACT.md`, the README's "Measuring impact in production" in Phase 4), one page, with the original plan's parts: how the tool would be put in front of support; a hypothesis (for example "the assistant resolves X% of transaction tickets without escalating to engineering"); primary and guard metrics: resolution without escalation, time to diagnosis, share of answers corrected by humans, reported hallucinations; the experiment: gradual rollout with a control group; and the criteria to scale or roll back.
- **R9. Demo.** The full demo (explain a success, a diagnosed failure, a follow-up question with a tool call, an unverified contract with degradation) SHALL run in the browser and be recorded.

- **R10. Cache by (network, hash).** THE SYSTEM SHALL keep each evidence bundle in the local SQLite, keyed by chain id and transaction hash plus a bundle format version (so a code change that alters facts invalidates old entries), and answer a repeated request from it. A bundle is kept only when its facts cannot change: the block is final for the network (the node's `finalized` block where supported, otherwise a configured number of confirmations) and it has no retryable gap (source unavailable, source behind, pending). A bundle with such gaps is not kept, so a later request tries the sources again. Written answers are kept apart, keyed by the bundle's digest, mode, language, prompt version and model. `--fresh` (and a field in the API) skips the cache. **Accept:** the second explain of a recorded hash makes no network request and gives the same evidence; a bundle with an unavailable source is not kept; a zkSync transaction not yet executed on L1 is not kept.
- **R12. The user's question.** The original plan's input is "hash + optional question + mode". WHEN a question comes with the hash (`explain --question "…"`, `POST /explain`), THE SYSTEM SHALL give it to the writer as the reader's question, so the answer addresses it first, from the same evidence and through the same answer check (D28); the question is the reader's text, never an instruction about the rules, and a question the evidence cannot answer is said to be unanswered with what is missing. The structured answer keeps it (`question`). **Accept:** on the real Celo balance failure, "why didn't my payment go through?" gets an answer that starts with the cause, cites the read, and passes the check.
- **R11. Batch.** `anychain batch <file>` SHALL explain a list of hashes with bounded parallelism, write one result per line (JSONL), resume from where it stopped, use the cache, and log every run in the event log. **Accept:** a list of recorded hashes, interrupted and resumed, gives each hash once.

## 6. Non-functional

- **Local only:** binds to 127.0.0.1; no authentication (out of scope in the case).
- **Latency:** the page shows the evidence first and the written answer when ready.
- **Cost:** the chat is limited in tool calls per turn (proposed: 3) and turns per session; the eval reports tokens. Whether the Claude Code backend reports the tokens it used is checked in T1, not assumed; if it does not, the report says so for that backend.
- **Privacy:** sessions in memory, logged in the local SQLite only.
- **Cache size:** bounded (proposed: oldest entries removed past a configured size); production would move it to PostgreSQL next to the event log (design only, D24).

## 7. Threats

- **Tool misuse by the model:** only the fixed read-only tools exist; parameters are validated (address and hash format, block numbers, paths inside the synced repo only); each call has the explanation's time budget.
- **Prompt injection from on-chain or repo text** (revert strings, NatSpec, file contents): given to the model only as data inside evidence; the validator blocks values not in the evidence.
- **Local server exposure:** bound to localhost.

## 8. Out of scope

No `git push`, no deploy, no auth, no cloud. The production queue and worker stay design only (D24). Triage questions, multi-transaction analysis, gas suggestions, `debug_traceTransaction`, the final README and the Dockerfile are Phase 4 (security notes moved into Phase 2.5, D39).

## 9. Approach

- `api.py` (FastAPI): routes over the same `build_bundle` and writer; sessions in memory; the page served as a static file.
- `chat.py`: the session (evidence bundle that grows), the tool list, the loop. Tool requests use a small JSON protocol the model writes in its answer, run by our code, so it works the same on Claude Code, Anthropic and OpenAI backends (decision D1 below).
- `src/anychain/web/index.html`: one page, plain JavaScript, served at `/` by the API.
- `events.py`: new columns (mode, diagnosis rule, ABI source, tokens, cache hit, feedback), migrated on open as in D28.
- `eval/cases.yaml`, `eval.py`, `anychain eval` and `anychain metrics`.
- `cache.py`: bundle and answer cache in SQLite; finality from the node (`eth_getBlockByNumber("finalized")`) or confirmations from config; `anychain batch` built on the acceptance script's resume logic.

## 10. Tasks (each ends in a commit with tests and a clean-context review)

1. T0 (R10, R11): bundle and answer cache, `--fresh`, `anychain batch`; cache hits recorded in the event log.
1. T1 (R6): event log columns (with cache hit) and `anychain metrics` with its SQL (cache hit rate among the queries).
2. T2 (R1, R5): API with `/explain` and `/health`.
3. T3 (R3, R4): chat session, tools, loop, validator on every answer; `/chat` and `anychain chat`.
4. T4 (R2): the web page.
5. T5 (R7): find and record the missing real cases (allowance, out of gas, custom error), `eval/cases.yaml`, `anychain eval` and its report.
6. T6 (R8): `docs/IMPACT.md`.
7. T7 (R12): the user's question in `explain`, the API and the structured answer.
8. T8: the 1,800-transaction acceptance run (300 per network, seven checks against the node; docs/ACCEPTANCE.md) on the final code, as PHASE2_5 D3 decided; any level A or B false fact is fixed with a test before the phase closes.
9. T9 (R9): the browser demo, recorded; architecture docs; Phase 3 report to Joabe.

## 11. Rollout and reversal

Local tool: new commands and a server; nothing changes for `explain`. The chat can be left unused.

## 12. Decisions for Joabe (each with a proposed default)

- **D1. How the model asks for tools.** The original plan said "Anthropic SDK tool use"; the writer now runs on Claude Code (D27), which has no plain tool-calling API (only its own tools or MCP servers). Proposed: a small JSON protocol in the model's answer ("to answer I need: read balanceOf of X at block N"), run by our code, the same on every backend, and the model itself keeps no tools at all. Alternative: expose the tools to Claude Code as an MCP server (more moving parts, different per backend).
- **D2. Missing eval categories.** Find real Ethereum transactions for an allowance revert, an out-of-gas failure and a custom error through the explorer. Proposed: search the explorer's recent failures by reason; if a category has no real case on Ethereum after a bounded search, take it from another network and say so in the report, never a made-up one.
- **D3. The page.** Proposed: one plain HTML page with vanilla JavaScript served by the API (the original plan), no framework.
- **D4. Where this spec lives.** Proposed: in the repo (`docs/specs/`), as Phase 2.
- **Decided on 2026-10-08 (Joabe):** starting a conversation from the reader's problem instead of a hash (find the transaction by the account and its recent transactions) stays in Phase 4, with the original plan's triage (4.5); the page of T4 starts from a hash.
- **D5. When a transaction is final enough to cache** (requested by Joabe on 2026-10-08). Proposed: the node's `finalized` block where the node supports it (Ethereum and most L2s); otherwise `cache.min_confirmations` in the network config (proposed 64); networks with extra steps (zkSync L1 status) are kept only once those steps are done.

## 13. Tests and tracing

| Requirement | Test (written before the code) | Evidence |
|---|---|---|
| R1 | API tests with the recorded transport (FastAPI test client) | pasted |
| R2 | page loads, calls the API, renders badges and gaps (browser check in T7) | recorded demo |
| R3 | chat loop on a real failure: tool request, new fact, cited answer; invalid tool requests refused | pasted |
| R4 | a chat answer with an invented value is retried, then withheld | pasted |
| R5 | each mode reaches the writer | pasted |
| R6 | metrics SQL on a seeded log; columns migrated on old logs | pasted |
| R7 | `anychain eval` on the recorded cases; report fields | report |
| R8 | the page exists and has the four parts | review |
| R9 | demo recording | link |
| R10 | second explain with no network request; not kept with retryable gaps, before finality, or zkSync not executed; format version change invalidates | pasted |
| R11 | batch interrupted and resumed on recorded hashes | pasted |
| R12 | a question on a real failure: answered first, cited, checked; a question the evidence cannot answer is said unanswered | pasted |
| T8 | acceptance run on 1,800 transactions: zero level A or B false facts | acceptance report |

## 14. How to verify

`uv run pytest -q`; `uv run anychain eval`; `uv run anychain serve` then the demo in the browser.

## 15. Definition of done

- [ ] D1 to D5 decided.
- [ ] T0 to T9 committed, each with tests and a clean-context review.
- [ ] The acceptance run (T8) has no level A or B false fact.
- [ ] `anychain eval` runs and the UI does the full demo (the original plan's criterion).
- [ ] Architecture docs updated; Phase 3 explained to Joabe in plain Portuguese.
