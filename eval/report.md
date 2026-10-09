# Evaluation report

Run 2026-10-09 10:02, commit 965a152 + uncommitted changes, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 84.0% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 9.5 s |
| Tokens sent (cache included) / received | 58236 / 10566 (12981 read from the cache) |
| Cost reported by the backend | 0.2892 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 5.7 | 3873/334 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 23/23 | 2/2 | 0/0 | 16.4 | 5223/1847 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 18/20 | 2/2 | 2/2 | 11.1 | 4813/1405 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 12/13 | 2/2 | 2/2 | 6.7 | 6523/630 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 9/12 | 1/1 | 2/2 | 6.2 | 4682/576 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 28/34 | 1/1 | 3/3 | 13.9 | 6385/1753 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 24/29 | 1/1 | 2/2 | 16.6 | 9624/2005 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 4/7 | 1/1 | 0/0 | 5.3 | 3822/373 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 5/5 | 2/2 | 0/0 | 4.7 | 3940/277 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 11/13 | 1/1 | 2/2 | 8.6 | 4837/607 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 14/21 | 1/1 | 2/2 | 9.6 | 4514/759 |
