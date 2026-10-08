# Acceptance criteria

Agreed with Joabe on 2026-10-07. A phase closes by this criterion, not by "no reviewer found anything".

## Fact levels

| Level | Facts | Bar |
|---|---|---|
| **A: core** | status, sender and recipient, value moved (native and tokens), total fee, decoded call when an ABI exists, failure reason | **zero false facts** |
| **B: network details** | L1 status, paymaster, fee flow, EIP-7702 delegations, explorer classifications, chain-type facts | **zero false facts**; may be declared "not interpreted" |
| **C: precision and context** | labels, wording, grouping, a detail that could be said better | not a defect; **each one goes to Joabe**, who decides: fix now or backlog |

## Sample

- **300 transactions per network** (6 networks, 1,800 in total), drawn at random: a random block from the last 30 days, then a random transaction in it. The seed is recorded so the sample can be reproduced.
- With zero false facts in 300 samples, the true rate is at most 1% per network with 95% confidence (rule of three: 3/n).
- Plus the recorded fixtures (`tests/fixtures`), which must all pass.

## How a sample is checked

Automatically, against an **independent source**: the network's JSON-RPC node, which neither the explorer nor our code controls.

1. Status matches the node's receipt.
2. Native value sent matches the node's transaction.
3. Total fee matches the receipt (gas used × effective gas price, plus the L1/blob parts where the network has them).
4. Token transfers match the `Transfer` events decoded straight from the receipt's logs (amount, from, to).
5. No native movement is stated twice as new money.
6. Every address quoted in a fact exists in the raw data the tool received.
7. zkSync only: L1 status, step by step. Each L1 step (commit, prove, execute) is proven on Ethereum through another provider (the L1 transaction succeeded and the zkSync contract logged that step for this batch). Every step the answer claims must match exactly; every step that existed when the answer was given (by L1 block time) must be stated, unless the answer declares the node check could not run; an answer that says nothing reached L1 fails if a step existed. Added after the 30-answer review found the gap in checks 1 to 6 (D25).

What the automatic checks flag is reviewed by hand (or by a clean-context reviewer). What they cannot check (e.g. the wording of a classification) is covered by a random sub-sample of 30 transactions reviewed in full.

## Review findings

- Level A or B: fixed, with a test that fails on the old code.
- Level C: listed for Joabe to decide.
- One review round per change.
