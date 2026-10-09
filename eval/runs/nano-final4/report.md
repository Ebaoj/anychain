# Evaluation report

Run 2026-10-09 08:36, commit 0b434ed + uncommitted changes, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 85.9% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 3.2 s |
| Tokens (input, output, cache read, cache write) | 1793, 5399, 33280, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.8 | 1981/189 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/6 | 2/2 | 0/0 | 5.9 | 3375/1121 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 8/8 | 2/2 | 2/2 | 3.7 | 3083/647 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 8/12 | 2/2 | 2/2 | 2.6 | 3840/338 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 6/6 | 1/1 | 2/2 | 3.1 | 2578/305 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 8/9 | 1/1 | 3/3 | 5.3 | 4322/950 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 8/9 | 1/1 | 2/2 | 4.8 | 6785/687 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 3/7 | 1/1 | 0/0 | 2.2 | 1940/274 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 3/3 | 2/2 | 0/0 | 1.8 | 2022/237 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 7/8 | 1/1 | 2/2 | 1.8 | 2693/278 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 7/7 | 1/1 | 2/2 | 2.5 | 2454/373 |
