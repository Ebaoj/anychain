# PHASE2_5: the gaps between Phase 2 and the case (before the interface of Phase 3)

- **Status:** DONE, approved by the author on 2026-10-08 (the three security-note wordings to the backlog as C25 to C27; the bare v4 Ownable text stays CONFIRMED and a no-reason failure with a passed deadline stays UNKNOWN, as D42 says)
- **Level:** full
- **Opened:** 2026-10-08
- **Project:** anychain (case 1.4, CloudWalk)
- **Type:** feature

## 1. Summary for the decision

- **What is missing:** a check of the case text (1.4) and the original plan against what Phases 1 and 2 built found items the case or the plan asks for that are not done. The two the case itself requires: the assistant cannot explain what a contract function does (the model never sees its code), and gives no security notes; and the ABI fallback to repo artifacts the case names was left out (D34). From the original plan: the structured JSON output, two diagnosis categories (access control; deadline from the decoded parameter), next steps per audience, and the CONFIRMED / LIKELY / UNKNOWN labels.
- **What changes for the user:** the answer can say what the called function does and where it failed, quoting its code; it adds heuristic security notes (clearly not an audit); access-control and expired-deadline failures are diagnosed and confirmed when a read can; next steps fit the reader (a merchant or a developer); every conclusion carries CONFIRMED, LIKELY or UNKNOWN; `explain --json` gives the structured answer the plan describes.
- **What does not change:** sourced facts only, the answer check, config-only networks, read-only.
- **Risk:** code given to the model is long and third-party text: it goes in as data inside the evidence, cut to the relevant function, and the validator still checks values. Security notes can sound like findings: they are labelled as pattern matches, never as vulnerabilities.
- **Decisions for the author:** section 12.

## 2. Context

- Case 1.4, task "Smart contract repo grounding": "explain contract/function behavior and provide relevant security notes"; requirement "ABI/decoding strategy: prefer explorer ABI, fallback to repo artifacts/ABIs, otherwise degrade gracefully".
- Original plan: section 1 principle 4 (confidence labels), 4.3 (diagnosis table, next steps adapted to the mode), 4.4 (structured JSON output; developer mode shows the code and the failing line; auditor mode gives security notes), 6.4 (security note patterns).
- What exists: repo and verified source indexed and cited by location (D33, D34), diagnosis rules (D31), confidence per fact (D29).

## 3. Problem (verified facts)

| # | Fact | Where | Consequence |
|---|---|---|---|
| G1 | Source facts give file, lines and a permalink, but not the code; the model's evidence has no function body | `bundle.py` `_cite_function` | it cannot explain what the function does, nor show the failing line |
| G2 | No security notes anywhere | none | the case's repo-grounding task is half done |
| G3 | `repo_artifacts` is accepted in config and does nothing | D34 | the case's named fallback is missing |
| G4 | `explain --json` prints the evidence bundle, not the plan's answer shape | `cli.py` | the API of Phase 3 has no answer schema to return |
| G5 | No access-control rule ("Ownable: caller is not the owner", AccessControl, their custom errors) | `diagnosis.py` | a common failure falls into "the contract's own reason" |
| G6 | Deadline is read from the reason text only, never from a decoded `deadline` parameter compared with the block time | `diagnosis.py` `_deadline` | it can only be LIKELY even when the call data proves it |
| G7 | Next steps are one text for every mode | `diagnosis.py` | a merchant reads developer advice and the reverse |
| G8 | Levels are confirmed / single_source / candidate; the plan's labels are CONFIRMED / LIKELY / UNKNOWN per conclusion | `models.py`, D29 | the plan's metrics ("% CONFIRMED / LIKELY / UNKNOWN") cannot be computed |

## 4. Objective

Close every gap above with real cases, so that Phase 3's interface shows the complete answer the case asks for.

## 5. Requirements

- **R1 (G1).** WHEN a called function, or the line that raises a failure's reason, is found in the deployed code's verified source (or a pinned repo), THE SYSTEM SHALL add its code as a fact (the function only, cut to a bounded size, with file, lines and source), so the model can explain behavior and the developer mode can quote the failing line. **Accept:** BRLC `setPauser` (D33) gives its body as a fact; Rootstock's "Too much requested" gives the `require` line of `V3SwapRouter.sol`.
- **R2 (G2).** THE SYSTEM SHALL scan the called function (and its contract) for the plan's patterns: `tx.origin` used for authorization, `delegatecall`, a low-level call whose result is not checked, an external call before a state update (reentrancy risk), a state-changing external function without an access modifier, `selfdestruct`, a loop over a storage array without a bound. Each match is a note citing the line, worded as "pattern found", always labelled a heuristic and not an audit; no match states nothing ("no notes" is not "safe").
- **R3 (G3).** THE SYSTEM SHALL read compiled ABIs from a repo's `artifact_globs` (Hardhat `artifacts/`, Foundry `out/`, `abi/` folders) and use them in the cascade after the explorer, for the pinned contract, before source signatures. They also cover struct parameters the source index leaves out. **Accept:** a real repo with committed ABIs decodes a real call with the explorer's ABI removed.
- **R4 (G4).** `explain --json` SHALL print the plan's shape: `summary` (the written answer, or null), `status`, `calls`, `transfers`, `events`, `diagnosis[]` (each with its label and cited fact ids), `next_steps[]`, `confidence`, `sources[]`, `gaps[]`, plus the evidence itself. The evidence bundle stays available (`--evidence`).
- **R5 (G5).** A failure whose reason is an access-control message (OpenZeppelin texts and custom errors, read in their source) SHALL be diagnosed as such, and CONFIRMED when a read at the parent block proves it (`owner()` is not the sender; `hasRole(role, sender)` is false).
- **R6 (G6).** WHEN the decoded call has a deadline parameter (by name, in the ABI), THE SYSTEM SHALL compare it with the block's timestamp and state the result; a deadline in the past is CONFIRMED.
- **R7 (G7).** Every finding SHALL carry next steps for a non-technical reader and for a developer; the writer uses the one that fits the mode (auditor: developer's).
- **R8 (G8).** Every diagnosis conclusion SHALL carry CONFIRMED (proven by a decoded revert reason or an on-chain read), LIKELY (inferred, the evidence named) or UNKNOWN (insufficient data, with what is missing), mapped from the existing levels and shown in the text, the JSON and the event log.

## 6. Non-functional

- Code facts are bounded (proposed: 80 lines per function) so the prompt stays small; tokens are reported by the eval in Phase 3.
- Security notes run on the source already indexed: no new network calls.

## 7. Threats

- **Code and comments as instructions:** contract code is third-party text. It is given as data inside a fact, never as instructions; the validator still blocks values not in the evidence.
- **False security alarms:** notes are pattern matches with the line cited, worded as such, and the auditor prompt says they are not findings.

## 8. Out of scope

No push, no deploy. Triage, multi-transaction analysis, gas suggestions and `debug_traceTransaction` stay in Phase 4 (the trace needs a node that supports it, which the private devnet of Phase 4 has). The interface is Phase 3.

## 9. Approach

- `solidity.py`: function text with line numbers; the pattern scan.
- `bundle.py`: code facts and security notes after the source citation; access-control reads.
- `collectors/repo.py`: artifact ABIs by contract name.
- `diagnosis.py`: access control rule, deadline from the decoded parameter, next steps per audience, the label mapping.
- `answer.py` (new): the structured JSON answer from the bundle and the written text.

## 10. Tasks (each ends in a commit with tests from real recordings and a clean-context review)

1. T1 (R1): function code as evidence; developer mode quotes the failing line.
2. T2 (R2): security notes.
3. T3 (R3): repo artifacts in the cascade.
4. T4 (R5, R6): access control rule and deadline from the decoded parameter, each with a real case.
5. T5 (R7, R8): next steps per audience and the CONFIRMED / LIKELY / UNKNOWN labels.
6. T6 (R4): structured JSON answer.
7. T7: architecture docs, decisions, report to the author; Phase 3 spec updated to build on this.

## 11. Rollout and reversal

Each addition is a new fact kind or rule; the answer check and the degradation rules are unchanged. Turning repos off (`repos: []`) removes code facts and notes.

## 12. Decisions for the author (each with a proposed default)

- **D1. Security notes now.** The original plan put them in Phase 4 as the second thing to cut; the case asks for them inside repo grounding. Proposed: build them here, and remove them from the cut list.
- **D2. Real cases to find.** Access control and deadline-from-parameter failures, and a public repo that commits compiled ABIs for a contract with real transactions. Proposed: search the explorers and GitHub; if a category has no real case after a bounded search, say so and test it on the closest real data, never on made-up data. Candidate for artifacts: Safe's published deployment ABIs (`safe-global/safe-deployments`), for the real Safe transaction already recorded.
- **D3. Acceptance run.** These changes add facts but do not change the ones the 7 automatic checks compare. Proposed: no new 1,800 run here; the next one runs at the end of Phase 3, with the eval.
- **D4. Labels.** Proposed mapping: CONFIRMED = a read proves it, or the explorer's decoded revert reason states it (the plan: "provado por revert reason decodificado ou por leitura on-chain"); LIKELY = a pattern or a replay suggests it; UNKNOWN = no reason and nothing to infer from, with what is missing.

## 13. Tests and tracing

| Requirement | Test (written before the code) | Evidence |
|---|---|---|
| R1 | BRLC setPauser body as a fact; Rootstock reason line | pasted |
| R2 | each pattern on small sources; one real contract | pasted |
| R3 | real repo ABIs decode a real call | pasted |
| R4 | JSON shape on real recordings | pasted |
| R5 | real access-control failure, read confirms | pasted |
| R6 | real deadline failure with the parameter | pasted |
| R7 | support and developer steps reach the right mode | pasted |
| R8 | labels on every recorded failure | pasted |

## 14. How to verify

`uv run pytest -q`; `uv run anychain explain <hash> --json` on the real cases; a live explain in developer and auditor mode.

## 15. Definition of done

- [x] D1 to D4 decided (2026-10-08, the proposed defaults).
- [x] T1 to T7 committed, each with tests and a clean-context review (T1 D38, T2 D39, T3 D40, T4 D41, T5 D42, T6 D43); every review found real errors, all fixed before the commit.
- [x] Phase 3 spec updated to build on this.
- [x] Phase 2.5 explained to the author in plain Portuguese and approved (2026-10-08).

Results: suite 792 tests; no real access-control failure found in 1,600 recent failures on five networks (D41), so that rule is tested on OpenZeppelin's exact texts only.
