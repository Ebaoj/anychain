# AnyChain Transaction Assistant

Explains and troubleshoots EVM transactions on any network. Switching networks means switching a YAML file.

> Work in progress (phase 1 of 4). Full README comes in phase 4.

## Quickstart (phase 1)

```bash
uv sync
# The written explanation uses the local Claude Code CLI by default (logged in, no API key).
# For a deployed service set llm.provider to anthropic or openai and export its API key (D27).
uv run anychain explain 0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952 \
  --config configs/ethereum-mainnet.yaml
# Same code, another network:
uv run anychain explain 0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921 \
  --config configs/optimism-mainnet.yaml --no-llm
uv run pytest
```

Flags: `--mode support|developer|auditor`, `--json` (evidence bundle), `--no-llm` (evidence only).

Tests run offline against real recorded responses (`tests/fixtures/`, recorded with `scripts/record_fixture.py`).
