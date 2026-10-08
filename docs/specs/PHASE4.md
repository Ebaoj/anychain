# PHASE 4: bonus features and finish

Status: **DRAFT** (2026-10-08). Delivery: 2026-10-09. Process (Joabe, 2026-10-08): one clean-context review for the whole phase, level C findings go to the backlog, level A and B stop the work.

## 1. Summary for the decision

The original plan's Phase 4 is "triage, multi-transaction, security notes, gas; final README, DECISIONS.md, diagram", with the cut order **gas, then security, then multi-transaction**, and **never cut: degradation, citations, eval, README**. Security notes are done (D39). Added later by Joabe: a private devnet on `home server` (ROADMAP, 2026-10-07) and a Dockerfile (ROADMAP, 2026-10-08).

## 2. Requirements

- **R1. Triage (plan 4.5).** Before concluding, the assistant asks up to `max_clarifying_questions` questions, **only when the answer changes the diagnosis**, in the three cases of the plan:
  - the input is not a valid hash, or is an address: ask which transaction, and list the address's recent transactions (explorer `GET /addresses/{address}/transactions`), each pickable;
  - the transaction succeeded but the reader says they did not receive it: ask which token and which address they expected, then answer from the transfers (received or not, to which address, which token);
  - several competing LIKELY hypotheses: ask what the reader was trying to do.
  The questions are chosen by code from the evidence (never by the model), so they are testable; the model only writes the final answer. In the API, `/explain` returns `{"clarify": {question, options}}` instead of an answer when one applies; the page shows the options as buttons; `anychain chat` asks in the terminal. **Accept:** each of the three cases on a real recording.
- **R2. Multi-transaction timeline (plan bonus 3).** For a failed transaction, fetch the sender's transactions around it (previous and next nonces, a bounded window) from the explorer, and add a `timeline` fact: each with its status, function and time, plus the patterns the plan names, said only when the data shows them: an `approve` after the failure followed by a success of the same call; several failures of the same call in a row. **Accept:** on a real sender with a failure then a success, the timeline and the pattern are facts with sources.
- **R3. Gas notes (plan bonus 5, small scope).** `gas_used / gas_limit`, the fee in the native coin (already a fact), and, from the called function's code shown (D38), a heuristic note for storage reads repeated in a loop. Comparison with similar transactions only when the timeline has the same call. Always labelled as notes. **Accept:** on the out-of-gas and a success recording.
- **R4. Final README (plan section 10)**, every part: what it is with the demo GIF; quickstart tested from a clean clone; configuration and "Retargeting to another network (e.g. CloudWalk)"; architecture diagram and the ABI cascade; **at least 3 real sample conversations** (success in support mode, diagnosed failure in developer mode with an `eth_call` read and a repo citation, unverified contract with degradation, and triage with a clarifying question); the eval table copied from `eval/report.md`; metrics and impact (docs/IMPACT.md); known limits and next steps; a transparent note on how AI (Claude Code) was used and which decisions were human.
- **R5. Dockerfile**: one image running the CLI and the API with a config mounted from outside, no secrets inside; built and run on `home server`. **Accept:** `docker run` serves `/health` on `home server`.
- **R6. Private devnet on `home server` (ROADMAP)**: Anvil plus a self-hosted Blockscout, the BRLC contracts deployed, real transactions (transfer, approve, paused revert, access-control revert, out of gas), then `configs/devnet.yaml` only. **Risk:** Blockscout needs Postgres and `home server` has 4 GB of RAM. **Proposed:** attempt it last, time-boxed to 2 hours; if it does not fit, the README says so and the design stays in ROADMAP.
- **R7. Architecture docs** (both languages) and DECISIONS.md updated; the Phase 4 report to Joabe in plain Portuguese.

## 3. Out of scope

No push, no deploy (the delivery itself is Joabe's call). `debug_traceTransaction` stays optional (`rpc.supports_debug_trace`): no public node used here offers it, and the devnet's Anvil would (only if R6 runs).

## 4. Tasks, in the order they are done

1. T1 (R1) triage, with tests on recordings.
2. T2 (R2) timeline.
3. T3 (R3) gas notes.
4. T4 (R5) Dockerfile on `home server`.
5. T5 (R4, R7) README, sample conversations run for real, docs.
6. T6 (R6) devnet, time-boxed.
7. One clean-context review of the whole phase; fixes for level A and B; then stop and ask about delivery.

If time runs short, the plan's cut order applies: gas first, then multi-transaction; R1, R4 and the review are never cut.

## 5. Decisions for Joabe

- **D1.** The devnet (R6) last and time-boxed to 2 hours, or cut now? Proposed: last and time-boxed.
- **D2.** `max_clarifying_questions` default 1 (one question, then answer)? Proposed: 1.
