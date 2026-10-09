# Evaluation report

Run 2026-10-09 13:33, commit 4d3728b, model gpt-4.1-mini. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 70.0% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 9.1% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 1 |
| Answers withheld | 0 of 11 |
| Time per written answer | 5.9 s |
| Tokens sent (cache included) / received | 45495 / 6211 (24064 read from the cache) |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 2.9 | 2390/162 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 11/11 | 2/2 | 0/0 | 8.4 | 3593/818 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 11/13 | 2/2 | 2/2 | 9.4 | 3400/837 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 5/7 | 2/2 | 2/2 | 2.7 | 4490/220 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 5/8 | 1/1 | 2/2 | 2.8 | 2987/259 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable | ok | 14/21 | 1/1 | 3/3 | 10.6 | 4246/897 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | retried | 8/20 | 1/1 | 2/2 | 17.4 | 13623/2050 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/5 | 1/1 | 0/0 | 2.5 | 2349/206 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 2.4 | 2431/197 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 5/10 | 1/1 | 2/2 | 2.8 | 3123/264 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 9/9 | 1/1 | 2/2 | 3.0 | 2863/301 |
