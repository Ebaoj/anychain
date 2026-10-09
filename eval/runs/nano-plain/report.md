# Evaluation report

Run 2026-10-09 09:40, commit de450c9 + uncommitted changes, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 67.4% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 93.8% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 3.3 s |
| Tokens (input, output, cache read, cache write) | 36928, 5384, 0, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/4 | 2/2 | 0/0 | 1.6 | 2240/201 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/6 | 2/2 | 0/0 | 5.0 | 3375/958 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 8/8 | 2/2 | 2/2 | 4.2 | 3083/587 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 5/8 | 1/2 | 2/2 | 2.8 | 4120/334 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 5/8 | 1/1 | 2/2 | 2.0 | 2837/269 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 8/9 | 1/1 | 3/3 | 5.5 | 4322/1080 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 9/10 | 1/1 | 2/2 | 3.9 | 6785/591 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/7 | 1/1 | 0/0 | 4.1 | 2199/322 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/8 | 2/2 | 0/0 | 3.0 | 2281/402 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 6/10 | 1/1 | 2/2 | 2.2 | 2973/279 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 4/11 | 1/1 | 2/2 | 2.2 | 2713/361 |
