# PHASE3: interface and evaluation (API, web UI, chat with tools, metrics, eval suite)

- **Status:** DRAFT (waiting for Joabe's approval)
- **Level:** full
- **Opened:** 2026-10-08
- **Project:** anychain (case 1.4, CloudWalk)
- **Type:** feature

## 1. Summary for the decision

- **What is missing:** the tool answers one question per command, on the command line only. There is no conversation, no web page, no API, no way to measure answer quality across a fixed set of cases, and no usage metrics beyond the event log.
- **What changes for the user:** a local web page and API. The user pastes a hash, picks a mode, gets the explanation with clickable sources, a confidence badge per conclusion and a "missing data" panel, then asks follow-up questions in a chat. In the chat the model can ask for more data (read contract state, open a repo file, look at another transaction), and every answer it gets becomes a new citable fact. `anychain eval` runs a fixed set of real cases and reports accuracy, citation coverage and hallucination rate; `anychain metrics` prints usage numbers from SQL.
- **What does not change:** facts stay sourced, the answer check (validator) runs on every chat answer, nothing is asserted without a source, the tool stays read-only, network changes stay config-only.
- **Risk:** a chat where the model chooses what to look up is where it can drift from the evidence; every tool result is a fact with a source, and every chat answer goes through the validator.
- **Decisions for Joabe:** section 12.

## 2. Context

- Phases 1 and 2 closed (D37): `anychain explain` builds a sourced evidence bundle with diagnosis, replay, repo source and signature candidates; the writer runs on Claude Code (D27) and its answer is checked (D28); an event log in SQLite (D24) counts problems per network.
- The original case plan, section 5 (interfaces), 7 (agent evaluation), 8 (metrics) and 9 (phase 3 "done when `anychain eval` runs and the UI does the full demo").

## 3. Problem (verified facts)

| # | Fact | Where | Consequence |
|---|---|---|---|
| P1 | Commands are `explain`, `log`, `repos sync`; no `chat`, `eval`, `metrics` | `cli.py` | the original interface list is not met |
| P2 | No web server or page; FastAPI is not a dependency | `pyproject.toml` | the case's "simple interface (web preferred)" is not met |
| P3 | Modes exist (support, developer, auditor: one prompt each) but only on the CLI | `prompts/`, `cli.py` | |
| P4 | The event log has no mode, diagnosis category, ABI source, tokens or user feedback | `events.py` runs table | the metrics the case asks for cannot be computed |
| P5 | No eval set: of the 8 categories the case requires on Ethereum, recorded real cases exist for 5 (ERC-20 transfer, DEX swap, explicit revert reason, unverified contract, node unavailable); none yet for an allowance revert, an out-of-gas failure, a Solidity custom error | `tests/fixtures/` | the eval cannot cover what the case asks |

## 4. Objective

A local API and web page with a chat whose every answer is grounded and checked, plus an evaluation command and a metrics command whose numbers come from real cases and SQL.

## 5. Requirements

- **R1. API.** THE SYSTEM SHALL serve `POST /explain` (hash, mode, optional question), `POST /chat` (session id, message) and `GET /health` (active network; status of explorer, node and LLM), locally, with FastAPI. **Accept:** `/health` on the Ethereum config reports the network and each source's state from a real probe; `/explain` returns the same evidence as the CLI for the same hash.
- **R2. Web page.** THE SYSTEM SHALL serve one static page (no frontend build) with: the active network at the top, hash field, mode selector, chat area, clickable sources, a confidence badge per conclusion, and a "missing data" panel when there are gaps. **Accept:** the full demo runs in a browser (R9).
- **R3. Chat with tools.** WHEN the user asks a follow-up question, THE SYSTEM SHALL let the model request a tool from a fixed list (read contract state with `eth_call` at a block, open a file or function from a configured repo, explain another transaction hash), run it, add each result to the session's evidence as a numbered, sourced fact, and answer citing it. Tools are read-only and limited per turn. **Accept:** on a real failed transaction, "what was the sender's balance before?" makes the model ask for the read, the read becomes a new fact, and the answer cites it.
- **R4. Every chat answer is checked** by the validator (D28) against the session's evidence, with the same retry and withhold rules.
- **R5. Modes** in API and UI (support, developer, auditor), using the existing prompts.
- **R6. Metrics.** THE SYSTEM SHALL record per answer: mode, diagnosis category and level, ABI source of the main call, latency, tokens (when the backend reports them), and optional user feedback (thumbs up or down from the page); `anychain metrics` SHALL print 3 to 4 SQL queries with their SQL (failure causes, diagnosis levels, ABI source share, satisfaction by mode).
- **R7. Eval.** `eval/cases.yaml` SHALL hold at least 8 real Ethereum cases and 2 of a second network, each recorded as a fixture, covering: ERC-20 transfer, DEX swap, explicit revert reason, allowance revert, out of gas, unverified contract, Solidity custom error, node unavailable. Each case states the expected status, diagnosis category, ABI source and entities that must appear. `anychain eval` SHALL run them offline (recorded data) through the whole pipeline including the LLM, and report: status and category accuracy, citation coverage (share of factual sentences citing evidence), hallucination rate (validator problems; target zero), share of degradation declared correctly, latency and tokens per case.
- **R8. Production impact page** (`docs/IMPACT.md`, for the README in Phase 4): hypothesis, primary and guard metrics, experiment design, criteria to scale or roll back. One page.
- **R9. Demo.** The full demo (explain a success, a diagnosed failure, a follow-up question with a tool call, an unverified contract with degradation) SHALL run in the browser and be recorded.

## 6. Non-functional

- **Local only:** binds to 127.0.0.1; no authentication (out of scope in the case).
- **Latency:** the page shows the evidence first and the written answer when ready.
- **Cost:** the chat is limited in tool calls per turn (proposed: 3) and turns per session; the eval reports tokens.
- **Privacy:** sessions in memory, logged in the local SQLite only.

## 7. Threats

- **Tool misuse by the model:** only the fixed read-only tools exist; parameters are validated (address and hash format, block numbers, paths inside the synced repo only); each call has the explanation's time budget.
- **Prompt injection from on-chain or repo text** (revert strings, NatSpec, file contents): given to the model only as data inside evidence; the validator blocks values not in the evidence.
- **Local server exposure:** bound to localhost.

## 8. Out of scope

No `git push`, no deploy, no auth, no cloud. The production queue and worker stay design only (D24). Triage questions, multi-transaction analysis, gas and security notes are Phase 4.

## 9. Approach

- `api.py` (FastAPI): routes over the same `build_bundle` and writer; sessions in memory; the page served as a static file.
- `chat.py`: the session (evidence bundle that grows), the tool list, the loop. Tool requests use a small JSON protocol the model writes in its answer, run by our code, so it works the same on Claude Code, Anthropic and OpenAI backends (decision D1 below).
- `web/index.html`: one page, plain JavaScript.
- `events.py`: new columns (mode, category, level, ABI source, tokens, feedback), migrated on open as in D28.
- `eval/cases.yaml`, `eval.py`, `anychain eval` and `anychain metrics`.

## 10. Tasks (each ends in a commit with tests and a clean-context review)

1. T1 (R6): event log columns and `anychain metrics` with its SQL.
2. T2 (R1, R5): API with `/explain` and `/health`.
3. T3 (R3, R4): chat session, tools, loop, validator on every answer; `/chat` and `anychain chat`.
4. T4 (R2): the web page.
5. T5 (R7): find and record the missing real cases (allowance, out of gas, custom error), `eval/cases.yaml`, `anychain eval` and its report.
6. T6 (R8): `docs/IMPACT.md`.
7. T7 (R9): the browser demo, recorded; architecture docs; Phase 3 report to Joabe.

## 11. Rollout and reversal

Local tool: new commands and a server; nothing changes for `explain`. The chat can be left unused.

## 12. Decisions for Joabe (each with a proposed default)

- **D1. How the model asks for tools.** The original plan said "Anthropic SDK tool use"; the writer now runs on Claude Code (D27), which has no plain tool-calling API (only its own tools or MCP servers). Proposed: a small JSON protocol in the model's answer ("to answer I need: read balanceOf of X at block N"), run by our code, the same on every backend, and the model itself keeps no tools at all. Alternative: expose the tools to Claude Code as an MCP server (more moving parts, different per backend).
- **D2. Missing eval categories.** Find real Ethereum transactions for an allowance revert, an out-of-gas failure and a custom error through the explorer. Proposed: search the explorer's recent failures by reason; if a category has no real case on Ethereum after a bounded search, take it from another network and say so in the report, never a made-up one.
- **D3. The page.** Proposed: one plain HTML page with vanilla JavaScript served by the API (the original plan), no framework.
- **D4. Where this spec lives.** Proposed: in the repo (`docs/specs/`), as Phase 2.

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

## 14. How to verify

`uv run pytest -q`; `uv run anychain eval`; `uv run anychain serve` then the demo in the browser.

## 15. Definition of done

- [ ] D1 to D4 decided.
- [ ] T1 to T7 committed, each with tests and a clean-context review.
- [ ] `anychain eval` runs and the UI does the full demo (the original plan's criterion).
- [ ] Architecture docs updated; Phase 3 explained to Joabe in plain Portuguese.
