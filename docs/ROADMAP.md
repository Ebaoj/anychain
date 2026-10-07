# Roadmap

Agreed items for later phases, so they are not lost between sessions. Each item says why it matters and what "done" looks like.

## Phase 4

### Private devnet that mirrors the CloudWalk setup
- **Why:** no public network reproduces CloudWalk's situation: a private node, a private Blockscout and its own contracts. This is the strongest proof that retargeting needs config only.
- **What:** on the `home server` server (Docker), run a local EVM node (Anvil) plus a self-hosted Blockscout. Deploy the BRLC contracts from the configured repo, generate real transactions (transfers, approvals, a paused-token revert, an access-control revert, an out-of-gas), then point AnyChain at it with a new YAML only.
- **Done when:** `anychain explain` and `anychain eval` run against the devnet with a `configs/devnet.yaml`, no code changes, and the README shows one sample conversation from it.
- **Risk:** Blockscout needs Postgres; `home server` has 4 GB RAM. Check memory before starting; fall back to the backend without the frontend.
- **Decided:** 2026-10-07, by Joabe.

## Open questions
- Anthropic API key for the LLM writer: own key (`ANYCHAIN_ANTHROPIC_API_KEY`) vs the Aulai key.
- Using the unofficial mirror `cloudwallk/brlc-token` in the demo, with a README note.
