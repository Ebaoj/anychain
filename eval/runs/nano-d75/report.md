# Evaluation report

Run 2026-10-09 13:32, commit 3a399db, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 77.0% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 93.8% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 3.6 s |
| Tokens sent (cache included) / received | 38657 / 5113 (17152 read from the cache) |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.5 | 2390/145 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/6 | 2/2 | 0/0 | 6.4 | 3593/963 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 8/8 | 2/2 | 2/2 | 4.1 | 3400/550 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 6/9 | 1/2 | 2/2 | 2.7 | 4490/307 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 6/10 | 1/1 | 2/2 | 3.2 | 2987/385 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable | ok | 8/9 | 1/1 | 3/3 | 6.1 | 4246/858 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 8/9 | 1/1 | 2/2 | 4.9 | 6785/625 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/8 | 1/1 | 0/0 | 2.9 | 2349/366 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 5/5 | 2/2 | 0/0 | 2.4 | 2431/261 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 5/10 | 1/1 | 2/2 | 3.0 | 3123/300 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 9/10 | 1/1 | 2/2 | 2.6 | 2863/353 |
