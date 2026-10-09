# Earlier evaluation runs

The official runs are `eval/report.md` (explanations, Claude) and `eval/intents.md` (the chat's classifier, gpt-4.1-nano). Kept here as evidence for decisions:

- `nano-d75`, `mini-d75`: the same eval with gpt-4.1-nano and gpt-4.1-mini on the same commit, the comparison behind the recommendation of gpt-4.1-mini (D76, docs/EVALUATION.md).
- `nano-final-d67`: gpt-4.1-nano with the D67/D68 prompts.
- `intents-claude`: the chat's classifier with Claude (100%).

The intermediate runs that measured each step of making small models reliable (D62) are in the git history.
