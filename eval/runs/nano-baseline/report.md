# Evaluation report

Run 2026-10-09 08:01, commit 10ea453, model gpt-4.1-nano. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 72.6% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 18.2% |
| Key values present in the answer | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 3 |
| Answers withheld | 0 of 11 |
| Time per written answer | 4.2 s |
| Tokens (input, output, cache read, cache write) | 40230, 6469, 3456, 0 |
| Cost reported by the backend | 0 USD |

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 7/9 | 2/2 | 3.4 | 2206/344 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | retried | 13/13 | 2/2 | 9.4 | 7263/1519 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 8/12 | 2/2 | 3.6 | 3321/587 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 9/12 | 2/2 | 4.8 | 4055/408 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 4/9 | 1/1 | 3.4 | 2759/359 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable, source_unavailable | ok | 8/12 | 1/1 | 3.8 | 4515/689 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 8/14 | 1/1 | 5.6 | 6916/814 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 6/8 | 1/1 | 2.8 | 2144/311 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 6/7 | 2/2 | 1.8 | 2247/275 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 8/11 | 1/1 | 2.3 | 2862/365 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | retried | 8/10 | 1/1 | 5.6 | 5398/798 |
