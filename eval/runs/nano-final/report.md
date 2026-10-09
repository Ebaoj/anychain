# Evaluation report

Run 2026-10-09 08:24, commit 0b434ed + uncommitted changes, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 81.0% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 9.1% |
| Key values present in the answer | 81.2% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 86.7% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 2 |
| Answers withheld | 0 of 11 |
| Time per written answer | 3.0 s |
| Tokens (input, output, cache read, cache write) | 4739, 5392, 34432, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.8 | 1948/141 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 8/10 | 2/2 | 0/0 | 6.9 | 3361/952 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 10/12 | 2/2 | 2/2 | 3.2 | 3069/604 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 5/5 | 1/2 | 2/2 | 1.2 | 3807/158 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 5/6 | 0/1 | 2/2 | 1.6 | 2545/228 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | retried | 9/14 | 1/1 | 1/3 | 9.1 | 8693/1974 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 9/11 | 1/1 | 2/2 | 3.3 | 6771/544 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/4 | 1/1 | 0/0 | 1.5 | 1907/187 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.4 | 1989/178 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 5/6 | 1/1 | 2/2 | 1.7 | 2660/231 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 4/5 | 0/1 | 2/2 | 1.5 | 2421/195 |
