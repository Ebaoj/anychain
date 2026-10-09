# Earlier evaluation runs

Each folder is one `anychain eval` run kept as evidence for a decision; the official run is `eval/report.md`.

- `nano-baseline`, `nano-prompt`, `nano-outline`, `nano-final`, `nano-final2` to `nano-final4`: gpt-4.1-nano, step by step, while making the answers reliable on a small model (D62): the prompt alone made it worse; the outline built from the facts made it work.
- `claude-final`, `claude-final2`: Claude after the D62 changes, to check that the larger model did not lose quality.
- `nano-plain`, `nano-plain2`: the merchant's plain-language prompt (D67), before and after removing a wording that misled the model.
- `nano-final-d67`: gpt-4.1-nano with the final prompts (D68), the number quoted in the README.
