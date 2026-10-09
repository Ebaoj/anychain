# Evaluation results

`anychain eval` replays 11 real transactions from their recordings (8 on Ethereum, 2 on Optimism, 1 on the private demo network; `eval/cases.yaml`) through the whole pipeline, the model included, and measures the answer. Latest run ([eval/report.md](../eval/report.md), per-case details in `eval/report.json`):

Run 2026-10-09 11:37, commit af9391b, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 85.1% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 12.2 s |
| Tokens sent (cache included) / received | 59248 / 10964 (12981 read from the cache) |
| Cost reported by the backend | 0.2973 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 11.4 | 3874/358 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 22/22 | 2/2 | 0/0 | 14.2 | 5554/2009 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 20/22 | 2/2 | 2/2 | 10.5 | 5280/1458 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 11/11 | 2/2 | 2/2 | 22.6 | 6854/578 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 10/13 | 1/1 | 2/2 | 7.7 | 4682/627 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable | ok | 29/33 | 1/1 | 3/3 | 24.0 | 6270/1891 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 25/31 | 1/1 | 2/2 | 15.6 | 9623/1997 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 5/8 | 1/1 | 0/0 | 6.6 | 3822/424 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 4.2 | 3940/261 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 11/16 | 1/1 | 2/2 | 8.6 | 4838/620 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 13/17 | 1/1 | 2/2 | 8.6 | 4511/741 |

Citation coverage varies between runs while the other metrics hold: 78.1% to 85.1% over the four runs of 2026-10-09 with the final prompts; the model writes differently each time. A fresh clone reproduces every metric except the written answer's (the facts-only run, `anychain eval --no-llm`, needs no model and gives the same accuracy columns).

The allowance category had no real case on a public network (the candidate found was an inner out-of-gas, D50, D51); its real case comes from the private demo network, where the explorer gives no reason and the replay on the node finds "insufficient allowance" (D60). "First drafts the check caught" counts answers the model had to rewrite because they stated a value or cited a fact not in the evidence: none in this run. Earlier runs showed one ("E624"), which turned out to be the check's mistake, not the model's: a shortened address ("0x52b2…E624") read as a citation; fixed (D62).

**Smaller models:** the same eval with gpt-4.1-nano (OpenAI's smallest model), on the same commit and prompts (`eval/runs/nano-final-d67`): the same diagnosis, 100% of the key values and key facts, no answer withheld, 80.3% of factual sentences citing a fact. Getting there took code, not a longer prompt: an outline built for each transaction from its facts, only the reader's own next steps, citations normalized, and the key facts enforced (D62, with each step measured). The merchant's plain language (D67) lets more jargon through with the small model than with a larger one.

**Acceptance against an independent source:** 1,800 randomly sampled transactions (300 on each of six networks), checked against the network's node: six checks each (status, value, fee, token transfers, no double-counted native value, addresses exist) plus the L1 status on zkSync, 11,100 checks in all: 11,008 passed, 88 had no node data to compare, 4 flagged. The 3 false facts found (a missing receipt called "pending", a blob fee left out on Gnosis) were fixed with tests on the recorded cases; the fourth is a token the explorer hides as scam ([docs/acceptance-report.md](acceptance-report.md)). The run used the Phase 3 code: the Phase 4 facts (timeline, gas notes, triage) come from the same explorer lists and are covered by tests on recordings, not by this run.
