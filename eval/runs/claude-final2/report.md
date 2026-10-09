# Evaluation report

Run 2026-10-09 08:35, commit 0b434ed + uncommitted changes, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 81.6% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 11.2 s |
| Tokens (input, output, cache read, cache write) | 22, 10263, 9509, 45007 |
| Cost reported by the backend | 0.2846 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 7.0 | 3355/171 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 24/24 | 2/2 | 0/0 | 14.0 | 5223/1888 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 18/19 | 2/2 | 2/2 | 11.5 | 4814/1394 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 6/10 | 2/2 | 2/2 | 8.7 | 5972/433 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 7/11 | 1/1 | 2/2 | 10.3 | 4159/486 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 30/35 | 1/1 | 3/3 | 15.0 | 6386/1852 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 24/32 | 1/1 | 2/2 | 16.9 | 9624/2089 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 5/6 | 1/1 | 0/0 | 8.5 | 3302/345 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 5/5 | 2/2 | 0/0 | 13.4 | 3422/302 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 12/16 | 1/1 | 2/2 | 9.0 | 4287/693 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 12/18 | 1/1 | 2/2 | 9.2 | 3994/610 |
