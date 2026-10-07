# Decisions

Each entry: decision, alternatives, reason, trade-off.

## D1. Second network is Optimism, not Base
- **Alternatives:** Base (spec's first suggestion), Sepolia.
- **Reason:** `base.blockscout.com` answers API calls with a Cloudflare 403 challenge (checked 2026-10-07, also with a browser-like User-Agent). Optimism's Blockscout (`explorer.optimism.io`) answers normally.
- **Trade-off:** none for the portability proof; Base can be re-added later as one more YAML file.

## D2. Optimism RPC is `mainnet.optimism.io`
- **Alternatives:** `optimism-rpc.publicnode.com` (same provider as the Ethereum config).
- **Reason:** publicnode refuses receipts for non-recent blocks on Optimism without a personal token ("Archive requests require a personal token").
- **Trade-off:** a different provider per network, which is exactly what config-only retargeting allows.

## D3. Decode with our own decoder, not Blockscout's pre-decoded fields
- **Alternatives:** use `decoded_input` / `decoded` returned by Blockscout.
- **Reason:** the ABI cascade (repo artifacts, source signatures, 4byte) needs a decoder we control. Using the same decoder for every source keeps one code path and lets each decoding report where its ABI came from.
- **Trade-off:** one extra request per contract (`/smart-contracts/{address}`), cached per run.

## D4. Proxies: merge proxy ABI with the implementation ABI
- **Reason:** USDC and most upgradeable tokens are proxies; the proxy's own ABI does not contain `transfer`. Blockscout lists `implementations`, which we follow.
- **Trade-off:** if two ABIs define the same selector, the implementation wins (it is added last).

## D5. Evidence is built without any LLM; the LLM only writes
- **Reason:** spec principle 2. Facts are numbered (`E1`, `E2`...) and each has a source: explorer page, explorer API endpoint, or JSON-RPC method + params. The LLM receives only this list.
- **Trade-off:** the LLM cannot add useful context from its own knowledge. That is intended.

## D6. Read-only `staticcall`s are grouped into one fact
- **Reason:** a single Uniswap swap produced 7 staticcalls (balance reads) that buried the real value movements.
- **Trade-off:** the individual reads are not listed; the explorer link still shows them.

## D7. Pending transactions stop early
- **Reason:** Blockscout returns mempool transactions with a "fee" that has not been paid. We now report "pending, nothing is final" plus a gap, instead of a misleading fee.

## D8. Tests replay recorded real traffic (custom httpx transport)
- **Alternatives:** respx mocks written by hand.
- **Reason:** `scripts/record_fixture.py` records every real response a run needs; `ReplayTransport` serves them back, and can simulate an outage of any host. No test data is invented, and tests run offline.
- **Trade-off:** fixtures are large (~300 KB each) because Blockscout responses are verbose.

## D9. No agent framework
- **Reason:** spec section 2. A plain call to the Anthropic SDK is easier to read and debug. Tool use for chat comes in phase 3, as a simple loop.
