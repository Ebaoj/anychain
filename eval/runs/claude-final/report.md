# Evaluation report

Run 2026-10-09 08:28, commit 0b434ed + uncommitted changes, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 67.0% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 9.1% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 86.7% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 1 |
| Answers withheld | 1 of 11 |
| Time per written answer | 11.3 s |
| Tokens (input, output, cache read, cache write) | 24, 10811, 8656, 49893 |
| Cost reported by the backend | 0.3095 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 6.3 | 3338/192 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 26/26 | 2/2 | 0/0 | 15.3 | 5208/1841 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 17/21 | 2/2 | 2/2 | 13.3 | 4797/1390 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 7/13 | 2/2 | 2/2 | 8.8 | 5954/496 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | withheld | 0/0 | 0/0 | 0/2 | 20.2 | 8367/1223 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 19/36 | 1/1 | 3/3 | 13.3 | 6369/1659 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 24/34 | 1/1 | 2/2 | 15.5 | 9607/2098 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/7 | 1/1 | 0/0 | 7.9 | 3285/306 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/7 | 2/2 | 0/0 | 7.5 | 3404/310 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 10/19 | 1/1 | 2/2 | 8.9 | 4269/702 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 8/18 | 1/1 | 2/2 | 7.3 | 3975/594 |
