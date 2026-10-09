# Private demo network

The proof that retargeting needs configuration only: a local EVM node and a self-hosted Blockscout, the BRLC token
from the configured repository deployed behind a proxy (as CloudWalk runs it), real transactions on it, and
AnyChain pointed at it with `configs/devnet.yaml` and no code change (D60).

Built on 2026-10-08 on a small Linux server (4 GB of RAM), everything bound to 127.0.0.1 there:

1. `docker compose -f compose.yml up -d`: Anvil (chain id 31337, a block every 2 s), Postgres, Redis and the
   Blockscout backend (`ETHEREUM_JSONRPC_VARIANT=anvil`, as Blockscout's own `docker-compose/anvil.yml`; only these
   four services, with memory limits). `compose.yml` reads Blockscout's `docker-compose/envs/common-blockscout.env`
   from a sparse clone of github.com/blockscout/blockscout next to it.
2. Compile BRLC (github.com/cloudwallk/brlc-token at 74a5498, `contracts/`) with forge, `foundry.toml` here, and
   OpenZeppelin `contracts-upgradeable` and `contracts` 4.9.6 under `lib/`; `Proxy.sol` goes in `contracts/devnet/`.
3. `run.sh` (in the foundry image, host network): deploys the implementation and the proxy, configures a minter and
   a pauser, and sends the transactions listed in `transactions.env`: a mint, a transfer, an approve, and four
   failures (an allowance exceeded, a transfer while paused, a non-owner calling `setPauser`, out of gas). Keys
   are Anvil's public test accounts, which exist only on that local network.
4. On the workstation: `ssh -N -L 14000:127.0.0.1:4000 -L 18545:127.0.0.1:8545 <server>`, then
   `uv run anychain explain <hash> --config configs/devnet.yaml`.

Three of the transactions are recorded in `tests/fixtures/devnet_*.json` (tests/test_devnet.py), so the tests run
without the network.

What it showed: the BRLC call decoded from the repository's source; Blockscout run without its internal-transactions
fetcher gives no revert reason, and the replay on the node at the parent block recovers it ("ERC20: insufficient
allowance", "Pausable: paused", "Ownable: caller is not the owner"); and one defect, fixed: a replayed
access-control reason produced no conclusion (D60).
