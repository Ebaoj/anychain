# Evaluation report

Run 2026-10-08 21:23, commit 3827d6c, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 77.2% |
| Hallucination rate (first attempt with values not in the evidence) | 10.0% |
| Key values present in the answer | 100.0% |
| Values the check caught on a first attempt | 1 |
| Answers withheld | 0 of 10 |
| Time per written answer | 11.7 s |
| Tokens (input, output, cache read, cache write) | 22, 10301, 14490, 43804 |
| Cost reported by the backend | 0.2812 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values |
|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 11/13 | 2/2 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 27/29 | 2/2 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 17/22 | 2/2 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 13/17 | 2/2 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | retried | 14/16 | 1/1 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 20/34 | 1/1 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 22/28 | 1/1 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 13/16 | 1/1 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 10/15 | 2/2 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 12/16 | 1/1 |
