# Evaluation report

Run 2026-10-09 07:33, commit 969fde3, model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 76.3% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 8.8 s |
| Tokens (input, output, cache read, cache write) | 22, 10386, 15974, 42140 |
| Cost reported by the backend | 0.2757 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 11/13 | 2/2 | 6.2 | 3704/529 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 25/26 | 2/2 | 10.8 | 5596/1498 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 14/19 | 2/2 | 9.9 | 5195/1148 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 13/19 | 2/2 | 7.3 | 6305/665 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 12/17 | 1/1 | 7.6 | 4448/723 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 22/33 | 1/1 | 12.1 | 6715/1482 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 22/31 | 1/1 | 12.0 | 9890/1590 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 11/15 | 1/1 | 9.1 | 3623/690 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 13/15 | 2/2 | 6.9 | 3770/646 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 14/20 | 1/1 | 8.0 | 4558/781 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 14/16 | 1/1 | 7.2 | 4332/634 |
