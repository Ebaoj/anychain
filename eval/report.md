# Evaluation report

Run 2026-10-09 11:14, commit 88155eb, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 81.1% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 10.4 s |
| Tokens sent (cache included) / received | 59249 / 10777 (12981 read from the cache) |
| Cost reported by the backend | 0.2954 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 5.1 | 3874/268 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 25/25 | 2/2 | 0/0 | 14.9 | 5552/1923 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 19/22 | 2/2 | 2/2 | 11.9 | 5281/1472 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 11/13 | 2/2 | 2/2 | 9.8 | 6855/572 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 9/14 | 1/1 | 2/2 | 6.8 | 4682/702 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable | ok | 27/32 | 1/1 | 3/3 | 15.9 | 6268/1778 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 25/31 | 1/1 | 2/2 | 15.9 | 9624/1941 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 4/8 | 1/1 | 0/0 | 6.6 | 3823/410 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 8.6 | 3940/258 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 10/14 | 1/1 | 2/2 | 8.2 | 4838/682 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 12/18 | 1/1 | 2/2 | 10.7 | 4512/771 |
