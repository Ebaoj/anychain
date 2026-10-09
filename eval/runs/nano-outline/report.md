# Evaluation report

Run 2026-10-09 08:22, commit 0b434ed + uncommitted changes, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 55.7% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 87.5% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 80.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 2.8 s |
| Tokens (input, output, cache read, cache write) | 34786, 4393, 0, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 2.2 | 1948/119 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/8 | 2/2 | 0/0 | 6.5 | 3361/1035 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 10/12 | 2/2 | 2/2 | 3.7 | 3069/622 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 4/5 | 1/2 | 2/2 | 1.4 | 3807/157 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 4/6 | 1/1 | 1/2 | 1.7 | 2545/247 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 2/12 | 1/1 | 1/3 | 3.3 | 4308/608 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 9/15 | 1/1 | 2/2 | 4.9 | 6771/788 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/4 | 1/1 | 0/0 | 1.7 | 1907/228 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.6 | 1989/176 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 0/6 | 1/1 | 2/2 | 2.0 | 2660/219 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 0/5 | 0/1 | 2/2 | 1.6 | 2421/194 |
