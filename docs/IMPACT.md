# Measuring impact in production

One page on how the assistant would be put in front of a support team, what it should change, how to measure that, and when to scale it or roll it back. Every number below that is not a measurement of this repo is a **target or an assumption to be measured**, marked as such.

## 1. How it reaches support

- **Where:** inside the support tool, next to the ticket. When a ticket carries a transaction hash (pasted by the customer or found from the account, Phase 4's triage), the agent sees the structured answer (`POST /explain`, schema `anychain.answer/1`): the cause with its label (CONFIRMED, LIKELY, UNKNOWN), the next steps for a non-technical reader, the missing data, and the sources.
- **Who reads it first:** the support agent, never the customer directly. The agent decides what to send; the assistant drafts. Customer-facing answers come only after the guard metrics below hold for a full rollout stage.
- **Escalation stays one click away:** the developer mode of the same answer (steps for a developer, code facts, reads) is what goes to engineering when the agent escalates, so engineering starts from the evidence instead of from zero.
- **Feedback:** the 👍/👎 on every answer is stored with that answer's run (`runs.feedback`), and a ticket reopened or escalated after an answer counts as a correction (below).

## 2. Hypothesis

> For tickets about a blockchain transaction, the assistant lets support resolve **more tickets without escalating to engineering**, **faster**, without raising the share of wrong answers sent to customers.

Assumed starting point, to be measured in the baseline weeks before rollout: the share of such tickets escalated today, and the median time from ticket open to first correct diagnosis. The target to test is **a 30% relative drop in escalations** of transaction tickets (an assumption, chosen as the smallest change worth the integration work; the experiment says whether it holds).

## 3. Metrics

| Kind | Metric | How it is computed |
|---|---|---|
| Primary | Resolution without escalation | transaction tickets closed by support without an engineering handoff, over all transaction tickets |
| Primary | Time to diagnosis | from ticket open to the first message that states the cause (support tool timestamps) |
| Guard | Answers corrected by humans | answers where the agent edited the cause, gave 👎, or the ticket was reopened for the same transaction |
| Guard | Reported hallucinations | answers flagged by an agent or engineer as stating something not true of the transaction; target zero, every one reviewed and turned into a test |
| Guard | Answers withheld or without a model | share of `withheld` and `unavailable` outcomes (`runs.writer` in the event log; a query to add to `anychain metrics`); a rise means the check or the model provider is failing |
| Health | Source availability | gaps by cause per network (`anychain log`): explorer or node down, explorer behind |
| Health | Cost and latency per answer | tokens and cost per run (`runs.cost_usd`, `runs.input_tokens`…), p50 and p95 duration; cache hit rate |

The tool already records what it can see (labels, rule, ABI source, cache, mode, tokens, cost, feedback: D45, `anychain metrics` prints each number with its SQL). Escalation, reopening and time to diagnosis come from the support tool, joined by a ticket id the integration would store with the run (a column not built yet).

What the offline eval shows today (eval/report.md, 10 real cases): 100% status and diagnosis accuracy, 0% hallucination on the first attempt, 76.6% of factual sentences citing a fact, 8.2 s and about 0.02 USD per written answer. Those numbers say the tool is ready for a pilot; they do not say it helps support, which only the experiment below can show.

## 4. The experiment

- **Unit:** the transaction ticket, randomly assigned when it is opened (not the agent, so every agent works both ways and agent skill does not bias the result).
- **Baseline:** two weeks with the assistant running in shadow (answers recorded, never shown) to measure the starting point and to check the guard metrics on real tickets before anyone sees an answer.
- **Rollout:** 10% of transaction tickets with the assistant, 90% control; then 25%, 50%, 100%, each stage at least one week and until the primary metric has enough tickets for its confidence interval to exclude zero or the minimum effect.
- **Per network:** a network whose source availability or guard metrics are worse stays at its current stage; the others move on.

## 5. Scale or roll back

| Decision | When |
|---|---|
| **Move to the next stage** | escalation drops (interval excludes zero), time to diagnosis does not rise, every guard metric within its limit |
| **Hold** | the primary effect is not yet clear, guards fine: collect more tickets |
| **Roll back the stage** | any confirmed hallucination sent to a customer; answers corrected by humans above the control's rate of corrected agent diagnoses; withheld or unavailable answers above 10% for a day |
| **Stop the network** | a source down for most of a day (the assistant shows its gaps, but support should not depend on it there) |

A rollback is a flag in the support tool: the answer stops being shown, recording continues, so the cause can be studied on the same tickets.

## 6. What it does not measure yet

- Answers to customers directly (only after the agent-facing stages).
- Phase 4's triage (finding the transaction from a description); until then only tickets with a hash are in the experiment.
- Long-term effects: fewer repeated tickets for the same cause, product fixes found from the failure causes the log counts (`anychain metrics` "failure causes" per network).
