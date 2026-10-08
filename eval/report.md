# Evaluation report

Run 2026-10-08 18:17, commit 8384b4e, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 76.6% |
| Hallucination rate (first attempt with values not in the evidence) | 0.0% |
| Key values present in the answer | 100.0% |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 10 |
| Time per written answer | 8.2 s |
| Tokens (input, output, cache read, cache write) | 20, 9201, 12830, 32978 |
| Cost reported by the backend | 0.2265 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values |
|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 10/12 | 2/2 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 26/26 | 2/2 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 22/25 | 2/2 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 9/13 | 2/2 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  - | ok | 9/12 | 1/1 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 18/29 | 1/1 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 21/31 | 1/1 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 12/16 | 1/1 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 10/15 | 2/2 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 10/13 | 1/1 |
