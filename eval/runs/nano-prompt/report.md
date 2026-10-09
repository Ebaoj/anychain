# Evaluation report

Run 2026-10-09 08:09, commit 0b434ed + uncommitted changes, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 41.9% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 9.1% |
| Key values present in the answer | 78.6% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 86.7% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 4 |
| Answers withheld | 1 of 11 |
| Time per written answer | 3.6 s |
| Tokens (input, output, cache read, cache write) | 34951, 6670, 3200, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/6 | 2/2 | 0/0 | 1.9 | 1989/217 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | withheld | 0/0 | 0/0 | 0/0 | 15.7 | 6740/2957 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 9/18 | 2/2 | 2/2 | 4.3 | 3066/781 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 4/5 | 1/2 | 1/2 | 2.1 | 3838/217 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 4/8 | 0/1 | 2/2 | 1.7 | 2542/213 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 0/26 | 1/1 | 3/3 | 4.3 | 4260/798 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 10/19 | 1/1 | 2/2 | 4.0 | 6661/713 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/6 | 1/1 | 0/0 | 1.4 | 1927/191 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/6 | 2/2 | 0/0 | 1.6 | 2030/235 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 4/6 | 1/1 | 2/2 | 1.4 | 2645/172 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 3/5 | 0/1 | 1/2 | 1.6 | 2453/176 |
