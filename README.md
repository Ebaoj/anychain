# AnyChain Transaction Assistant

An assistant that **explains a blockchain transaction** to someone non-technical (a merchant: "why didn't my payment go through?") and to someone technical (a developer, an auditor), on **any EVM network**, by changing only a configuration file.

![The page as a conversation: a merchant pastes a failed payment with a question, gets the answer with its proof and asks a follow-up; then the developer view with the facts beside the thread, and the settings](docs/demo/anychain_demo_chat.gif)

## In one minute

The idea behind everything: **the AI finds nothing out; it only writes.** The code finds things out, and code can be tested.

```mermaid
flowchart LR
    Q["1. Question<br/>hash or address"] --> F["2. Facts<br/>no AI, numbered,<br/>with sources"]
    F --> D["3. Conclusion<br/>no AI, rules +<br/>reads on the node"]
    D --> W["4. Prose<br/>AI, cites<br/>every fact"]
    W --> C["5. Check<br/>no AI: every number<br/>must be in the facts"]
    C --> R["6. Answer<br/>page, CLI, API,<br/>chat"]
```

Labels: **CONFIRMED** (proved), **LIKELY** (the best reading of the facts), **UNKNOWN** (not enough data, said why). A check failure means one retry, then the answer is withheld and the facts are shown.

- **If the AI makes a mistake, the check catches it.** A number that is not in the facts never reaches the reader.
- **If a rule makes a mistake, a test finds it.** The evaluation found a rule that ignored an inner call running out of gas; the rule was fixed and the real case became a test (D50, D51).
- **When data is missing, the answer says what is missing** ("the explorer did not answer; try again") instead of guessing.
- **Another network is another YAML file:** configs for seven networks ship (plus a CloudWalk template), among them a private one built like CloudWalk's.

| Who | What they get |
|---|---|
| Merchant | plain words: did it work, did the money move, the fee, what to do now; the proof on demand |
| Developer | the decoded call, the function's code, reads on the node, the sender's timeline, gas notes; every fact with its source |
| Auditor | security notes first (heuristics, never an audit), who may call the function, what could not be checked |

Interfaces: a **web page** (a conversation), a **CLI** (`explain`, `chat`, `batch`, `eval`, `metrics`) and a **local API** (`/explain`, `/chat`).

---

## Start here (for a reviewer with an hour)

1. Watch the GIF above, then run the quickstart below (a few minutes) and open the page.
2. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 0 ("What this is, in one minute") and section 2 ("How the diagnosis decides").
3. Read the code in this order, each a few hundred lines at its core: `src/anychain/service.py` (one answer, end to end), `bundle.py` (`BundleBuilder.build`: how facts are collected), `diagnosis.py` (`diagnose` and the rules list), `validator.py` (the check on the AI's answer), `writer.py` (the prompts and the three model backends).
4. Skim the sample conversations (section 4) and the evaluation table (section 5).
5. For any choice that looks odd, search [docs/DECISIONS.md](docs/DECISIONS.md) for its number (D1 to D69): each says why, with the real transaction behind it.

---

## 1. Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). The written answer needs a model: the local [Claude Code](https://claude.com/claude-code) CLI (the default, logged in, no key), or an Anthropic or OpenAI API key, saved once with `anychain llm set` (or the page's **Model** panel) and kept outside the repository, readable by your user only. Without a model, everything still runs and shows the facts only.

```bash
git clone https://github.com/Ebaoj/anychain.git && cd anychain
uv sync

# download the configured contract repositories, pinned to their commits (once; `explain` only reads this local copy)
uv run anychain repos sync --config configs/ethereum-mainnet.yaml

# choose the model (skip if Claude Code is installed and logged in); the key is asked for, not shown
uv run anychain llm set --provider anthropic            # or openai; then pick the model from the provider's list
uv run anychain llm test --config configs/ethereum-mainnet.yaml

# CLI: explain a transaction (a USDC transfer on Ethereum)
uv run anychain explain 0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952 \
  --config configs/ethereum-mainnet.yaml

# the same code on another network, facts only (no model)
uv run anychain explain 0x7db4433fc318dfcf4a8d07022aec6135a5adb69b227f5a82e3092b3a648b0921 \
  --config configs/optimism-mainnet.yaml --no-llm

# a failure, with a question, as JSON
uv run anychain explain 0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a \
  --config configs/ethereum-mainnet.yaml --mode developer --question "why did it fail?" --json

# API + web page on http://127.0.0.1:8000 (this machine only: the API has no authentication); see "The page" below
uv run anychain serve --config configs/ethereum-mainnet.yaml

# follow-up questions in the terminal
uv run anychain chat 0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a --config configs/ethereum-mainnet.yaml

# evaluation: the facts-only run needs no model; the full run (drop --no-llm) has the model write every answer.
# --out keeps the committed eval/report.md (the table in section 5) untouched
uv run anychain eval --no-llm --out data/eval

# usage metrics, and the tests (offline, on real recordings; about a minute, longer on a busy machine)
uv run anychain metrics --config configs/ethereum-mainnet.yaml
uv run pytest -q
```

With Docker (no secrets in the image; the port is published on the host's 127.0.0.1 only):

```bash
docker build -t anychain .
docker run --rm -p 127.0.0.1:8000:8000 anychain                                   # API + page, Ethereum
docker run --rm anychain anychain explain 0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952 --no-llm
```

Do not run it with `--network host`: inside the container the API listens on 0.0.0.0, which with the host's network means every interface of the host. In a container the Claude Code CLI is not available: for a written answer set `llm.provider: anthropic` (or `openai`) in a mounted config and pass the key as an environment variable at run time.

**The page** (`anychain serve`, then http://127.0.0.1:8000) is a conversation, in three versions chosen on its first screen:

- **Merchant** (`#/lojista`, also `#/merchant`): plain words, the answer at a glance (worked or not, value moved, the fee rounded, the date in the reader's time zone), what to do now; the proof slides in on demand.
- **Developer** (`#/desenvolvedor`, also `#/developer`) and **Auditor** (`#/auditor`): the facts and their sources always beside the thread; the auditor sees the security notes first. A "facts only, no AI" switch.

The reader writes as they like ("meu pagamento 0x… não passou"): a transaction hash or an address anywhere in the message starts an explanation, the rest of the message is the question; an address lists its transactions as buttons; the clarifying question, when there is one, comes as a message with buttons. Any other message is a follow-up on the same transaction, answered by the chat with tools (a read on the node, a function's code, another transaction), each result a new sourced fact. Every citation `[E#]` opens its fact. Each network's config offers real example transactions.

**Settings** (⚙): the **network** (any of `configs/`, switched without restarting), **add a network** (chain id, coin, Blockscout explorer, node; "Test the connection" checks that the node answers on that chain id; saved on this computer only, in `~/.config/anychain/networks/`, mode 0600, because a node's address can carry a key; it is never sent back to the page), the **model** (Anthropic, OpenAI or local Claude Code; the key is saved on this computer and never shown again; the model is picked from the provider's own list for that key) and the **language** of the answers and the page (pt-BR, en, es). The settings endpoints answer only requests from the page itself (a custom header and an Origin check).

**Interfaces:** `anychain explain <hash> [--mode support|developer|auditor] [--config path] [--json] [--evidence] [--question "…"] [--no-llm] [--fresh]`, `anychain chat`, `anychain batch <file>`, `anychain serve`, `anychain eval`, `anychain metrics`, `anychain log`, `anychain repos sync`, `anychain llm show|set|test|clear`. API: `POST /explain` (hash, mode, question?, clarified?), `POST /chat` (session_id), `POST /feedback`, `GET /health` (active network; explorer, node and model status), `GET/POST /settings/llm`, `POST /settings/llm/models` (the provider's list for a key) and `POST /settings/llm/test` (the model and its key; the key is never returned), `GET/POST /settings/network` (list and switch), `GET /settings/network/draft`, `POST /settings/network/check` and `POST /settings/network/save` (add a network), `GET/POST /settings/answers` (language).

---

## 2. Configuration, and retargeting to another network (e.g. CloudWalk)

Everything network-specific is in one YAML file (`configs/*.yaml`); there is no network value in the code (a test enforces it: no explorer or node host, chain id, symbol or address; the only fixed URLs are links to the source of a rule's meaning, such as Uniswap's and OpenZeppelin's code, D31). Eight configs ship (seven networks and a template): Ethereum, Optimism, Gnosis, Rootstock, Celo, zkSync Era, the private demo network, and the CloudWalk template.

```yaml
network:    { name, chain_id, native_symbol, native_decimals, chain_type }   # chain_type = the explorer's Blockscout CHAIN_TYPE
explorer:   { type: blockscout, base_url, api_path: /api/v2, tx_url_template, address_url_template, timeout_s }
rpc:        { url, timeout_s, supports_debug_trace }                         # read-only JSON-RPC
repos:      [ { url, ref, source_globs, artifact_globs } ]                    # contract sources and ABIs, pinned
abi_strategy: { order: [explorer, repo_artifacts, repo_source_signatures, signature_db] }
llm:        { provider: claude_code | anthropic | openai, model, max_tokens }  # `anychain llm set` overrides it
assistant:  { default_mode, language, max_clarifying_questions, time_budget_s }
storage:    { sqlite_path, cache_dir }
```

**Retargeting to CloudWalk, step by step** (`configs/cloudwalk.example.yaml` is the commented template):

1. Copy the template to `configs/cloudwalk.yaml`.
2. `network`: chain id **2009** (mainnet) or **2008** (testnet), native currency **CWN** (public in chainid.network); `chain_type` as the internal Blockscout runs it (AnyChain warns when the explorer's answers look like another type).
3. `explorer.base_url` and `rpc.url`: the internal addresses (the public explorer names do not answer from outside). Keep `?app=anychain` on the RPC URL: Stratus can reject unidentified clients.
4. `repos`: the BRLC contracts, pinned to a commit. The template, like `ethereum-mainnet.yaml` and `devnet.yaml`, points to `github.com/cloudwallk/brlc-token`, an **unofficial public copy** (the organization is not CloudWalk's; the official repository went offline in 2024); prefer the internal official one. `ethereum-mainnet.yaml` also pins Uniswap's official V2 router source (`Uniswap/v2-periphery`), so a router call cites the repository's lines next to the explorer's verified code (D69). The other five configs set no repository: their facts come from the explorer and the node.
5. `signature_db.enabled: false` if policy forbids sending selectors to a public service.
6. `uv run anychain repos sync --config configs/cloudwalk.yaml`, then `uv run anychain serve --config configs/cloudwalk.yaml`. `GET /health` confirms the explorer, the node (and that its chain id matches) and the model.

No code change is needed: the same steps took the tool from Ethereum to five other networks of five different types, and to a **private network built like CloudWalk's**: a local node and a self-hosted Blockscout with the BRLC token from the repository deployed behind a proxy, read with `configs/devnet.yaml` only ([devnet/README.md](devnet/README.md)). There the BRLC call was decoded from the repository's source, and the revert reasons the explorer did not give were recovered by replaying the call on the node.

---

## 3. Architecture

The six steps of "In one minute", in more detail:

- **Collectors:** the explorer (Blockscout API v2) and the network's node (JSON-RPC) are read in parallel, each request bounded by a time budget; every failure becomes a **gap** with a cause and a "worth retrying" flag, never an error on screen. The node checks the explorer (status, receipt, gas); a network-type profile handles fees, L1 status and deposits per chain type.
- **Decoder and ABI cascade:** explorer ABI (proxies and EIP-7702 delegates followed) → repo artifacts (the address checked against the artifact's own list) → signatures from repo sources → a public signature database (candidates only, never facts) → raw data, declared.
- **Diagnosis:** finds where a failure began (an inner call's execution error first), reads the reason at the top, proves it with reads on the node at the parent block (balance, allowance, owner, roles, paused, decimals), and checks that the conclusion explains every failure signal; a signal left over lowers CONFIRMED to LIKELY. Next steps for a non-technical reader and for a developer.
- **Context facts:** the called function's verified code (numbered lines, the reason's line), heuristic security notes, the sender's timeline around a failure (retries, approvals, repeated failures), gas notes.
- **Writer and check:** one prompt per mode; the model receives only the facts and the gaps (no API or node addresses); every number, address, hash, link and citation in its answer must be in the evidence (a number may be rounded or cut to the precision written, "cerca de 0,00012 ETH" for a fee of 0.00011683874115936 ETH, but never changed; D28), else it retries once, else the answer is withheld and the facts are shown.
- **Chat with tools:** the model asks for data in JSON (read a contract's state, show a function's code or a repo file, look at another transaction); our code checks and runs the request and adds the result as a new sourced fact.
- **Storage:** SQLite event log (one row per answer: gaps by cause, label, ABI source, cache, mode, tokens, cost, feedback) and a cache by network and hash for final, complete transactions.

The full diagrams (pipeline, diagnosis, one `explain`, the chat, quality, code map, gap classification, metrics) are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); every decision, with the real case behind it, is in [docs/DECISIONS.md](docs/DECISIONS.md) (D1 to D69).

---

## 4. Sample conversations

Real transactions replayed from their recordings and answered by the model through the same path as `explain` and the API; generated by `scripts/sample_conversations.py`, not edited by hand (also in [docs/SAMPLES.md](docs/SAMPLES.md); here the answers' own headings are shown as bold lines). Samples 2, 3 and 7 are in English; the others in Portuguese, the configs' `assistant.language` (the page switches the language). Under each answer, the sources of every fact it cites. No single recorded failure has both an `eth_call` read and a repository citation: the second sample shows the reads, the third and the sixth show configured repositories.

| Sample | What it shows |
|---|---|
| 1. A successful ERC-20 transfer, support mode | the merchant's plain answer to a USDC transfer that worked |
| 2. A failure diagnosed with reads on the node (eth_call), developer mode | a failure proved with reads on the node: the sender's balance at the block before |
| 3. A failure diagnosed from the contract's verified code and the rule's repository source, with the sender's timeline, developer mode | a failure explained from Uniswap's configured repository (a permalink to the function's lines, checked against the verified code), with what the sender did next |
| 4. A contract the explorer has not verified (degradation), with a clarifying question (triage) | an unverified contract: what is missing is said, and one question is asked first |
| 5. Triage: it succeeded, but the reader says the payment did not arrive | the reader says "I did not receive it": their words checked against the facts |
| 6. The private demo network: a BRLC call decoded from the configured repository, and the cause the explorer did not give recovered by replaying the call on the node, developer mode | the private network: CloudWalk's BRLC decoded from its repository, the hidden cause recovered by replay |
| 7. An auditor's review, in English: a security note on the called function's code (heuristic, not an audit) | the auditor's view, in English: who may call the function, and a security note marked as a heuristic |

Click a sample to open it.

<details><summary><strong>1. A successful ERC-20 transfer, support mode</strong></summary>

Network `ethereum-mainnet`, transaction `0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952`, mode `support`.

**Assistant:**

**O que aconteceu**

A transação deu certo. Ela foi concluída em 7 de outubro, às 15h47 UTC, e não falhou [E1]. Nenhum valor em ETH foi enviado nela [E1].

**O dinheiro foi movimentado?**

Sim. Foram enviados 69.3484 USDC do endereço 0xCA62C34d54b445283121905F19193416212117c4 para o endereço 0x74AA5387681505c806FF1E972B12cdfd01406828 [E5].

**Taxa**

A taxa de rede paga foi de cerca de 0,000085 ETH (0.00008537619972948 ETH) [E2].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://eth.blockscout.com/tx/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952)
- **E2** (fee): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952)
- **E5** (token_transfer): [Explorer API: token transfers](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952/token-transfers)

</details>

</details>

<details><summary><strong>2. A failure diagnosed with reads on the node (eth_call), developer mode</strong></summary>

Network `celo-mainnet`, transaction `0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817`, mode `developer`, answer language `en`.

**Assistant:**

**Summary**
The transaction failed (reverted) in block 78962884 at 2026-10-01T14:07:22.000000Z. It was sent from 0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0 to 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e (Tether USD), with 0 CELO attached [E1]. Explorer page: https://celo.blockscout.com/tx/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817 [E1].

**Fee**
The fee paid was 0.008222877 CELO, and it is charged even though the transaction failed [E2].

**Decoded call**
The call was transfer(address,uint256) on 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e (Tether USD) [E4]. The arguments were recipient=0x14634De7D71618013Dc48F2e85E80A28023f4367 and amount=106500 [E4]. The ABI came from the explorer, from a verified contract: TransparentUpgradeableProxy proxy -> TetherTokenCeloExtension [E4].

**Movements**
No token transfer is described in the facts. The only movement listed is an internal delegatecall from 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e (Tether USD) to 0xBF83F8436Ac46A8B1da5A9348eD84F68aEe07B98 (TetherTokenCeloExtension), which failed with "execution reverted" [E6].

**Cause**
- CONFIRMED: at block 78962883, the block before this transaction, the balance of 0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0 was 1445 raw units (0.001445 USD₮). That is less than the 106500 raw units (0.1065 USD₮) the call asked for [E10].
- The explorer reports the revert reason as Error(string reason) with reason='ERC20: transfer amount exceeds balance' [E3].
- That reason is written in the verified source @openzeppelin/contracts-upgradeable/token/ERC20/ERC20Upgradeable.sol, line 236 [E12].
- The symbol "USD₮" is the name the contract gives itself, not proof of which token it is [E10].

**Code**
- The shown transfer function (ERC20Upgradeable.sol, lines 117-120) calls `_transfer(_msgSender(), recipient, amount)` and then returns true [E5].
- The shown `_transfer` (lines 225-245) first requires that sender and recipient are not the zero address (lines 230-231) [E11].
- It then calls `_beforeTokenTransfer`, reads `_balances[sender]`, and at line 236 requires `senderBalance >= amount`, with the message "ERC20: transfer amount exceeds balance" [E11].
- If that check passes, it subtracts the amount from the sender, adds it to the recipient, emits Transfer, and calls `_afterTokenTransfer` [E11].
- This source is from the implementation the explorer lists today, which may have been upgraded since this transaction [E5][E11].
- The failure was most likely raised at line 236, unless it was passed on from another function or contract that was called [E11].

**Timeline**
The same transfer call failed at least 35 times in a row, with no other transaction from the sender in between (nonces 54482 to 54516) [E14]. This transaction is one of them.

**Gas**
Heuristic note, not a finding: gas used was 33839 of the 100000 limit (33.8%) [E15].

**Next steps**
For a developer:
- Check the balance of the account the tokens are moved from before sending, and move at most that [E10].
- If the balance was expected, check for an earlier transaction that spent it [E10].

**What is missing**
The sender sent at least 150 transactions after this one, and those right after it are not shown. To see them, open the sender's page on the explorer [E13].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://celo.blockscout.com/tx/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817), [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817)
- **E2** (fee): [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817)
- **E3** (revert): [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817)
- **E4** (call): [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817), [Explorer API: contract ABI](https://celo.blockscout.com/api/v2/smart-contracts/0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e)
- **E5** (code): [Explorer API: verified source](https://celo.blockscout.com/api/v2/smart-contracts/0xBF83F8436Ac46A8B1da5A9348eD84F68aEe07B98)
- **E6** (internal_call): [Explorer API: internal transactions](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817/internal-transactions)
- **E10** (diagnosis): [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817), [JSON-RPC](https://forno.celo.org), [JSON-RPC](https://forno.celo.org), [JSON-RPC](https://forno.celo.org)
- **E11** (code): [Explorer API: verified source](https://celo.blockscout.com/api/v2/smart-contracts/0xBF83F8436Ac46A8B1da5A9348eD84F68aEe07B98)
- **E12** (source): [Explorer API: verified source](https://celo.blockscout.com/api/v2/smart-contracts/0xBF83F8436Ac46A8B1da5A9348eD84F68aEe07B98)
- **E13** (timeline): [Explorer API: the sender's transactions](https://celo.blockscout.com/api/v2/addresses/0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0/transactions?filter=from)
- **E14** (timeline): [Explorer API: the sender's transactions](https://celo.blockscout.com/api/v2/addresses/0x9695B8367fd1Bb4800667Ff5F35B0CF142F410A0/transactions?filter=from)
- **E15** (gas_note): [Explorer API: transaction](https://celo.blockscout.com/api/v2/transactions/0x9a8b0c69355a900965e9d3ddb4f4a7d2bb8822872d36e3ba03b55e4e7f419817)

</details>

**Missing data the answer declares:**

- Timeline: the sender sent at least 150 transactions after this one; those right after it are not shown

</details>

<details><summary><strong>3. A failure diagnosed from the contract's verified code and the rule's repository source, with the sender's timeline, developer mode</strong></summary>

Network `ethereum-mainnet`, transaction `0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a`, mode `developer`, answer language `en`.

**Assistant:**

**Summary**
The transaction failed (reverted) in block 26149091, sent from 0x79D6C25dE86bC4A858d7e383A474667aB109FeD0 to 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D (UniswapV2Router02) [E1]. The RPC receipt independently reports status failed, matching the explorer [E9].

**Fee**
The fee paid was 0.00011683874115936 ETH, and it is charged even though the transaction failed [E2].

**Decoded call**
- Function: swapExactETHForTokens(uint256,address[],address,uint256) on 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D (UniswapV2Router02) [E4].
- amountOutMin = 0 [E4].
- path = [0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2, 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48] [E4].
- to = 0x79D6C25dE86bC4A858d7e383A474667aB109FeD0 [E4].
- deadline = 2400 [E4].
- The ABI source is the explorer's verified contract UniswapV2Router02 [E4].

**Cause**
- CONFIRMED: the call's deadline parameter is 2400 (1970-01-01T00:40:00Z) and the block's time is 1791479507 (2026-10-08T17:11:47Z). The deadline had passed when the transaction was included, and the reason 'UniswapV2Router: EXPIRED' is Uniswap's deadline check [E7].
- The explorer reports the revert reason as Error(string reason) with reason='UniswapV2Router: EXPIRED' [E3].
- The function is defined in contracts/UniswapV2Router02.sol, lines 252-266, in Uniswap/v2-periphery@ed24991, and its text is the same in the explorer's verified source (comments and spacing aside) [E5].
- The reason 'UniswapV2Router: EXPIRED' is written in contracts/UniswapV2Router02.sol, line 19 [E8].

**Code**
Only the lines shown (252-266) are described:
- The function is external, virtual, override and payable, and it uses the modifier `ensure(deadline)` [E6].
- It requires `path[0] == WETH`, otherwise it reverts with 'UniswapV2Router: INVALID_PATH' [E6].
- It computes `amounts` with `UniswapV2Library.getAmountsOut(factory, msg.value, path)` [E6].
- It requires the last amount to be at least `amountOutMin`, otherwise it reverts with 'UniswapV2Router: INSUFFICIENT_OUTPUT_AMOUNT' [E6].
- It calls `IWETH(WETH).deposit{value: amounts[0]}()`, asserts the WETH transfer to the pair address, and then calls `_swap(amounts, path, to)` [E6].

**Timeline**
The same call (swapExactETHForTokens to 0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D) succeeded 48 seconds later (nonce 53), the first success after this failure [E11].

**Gas**
These notes are heuristic, not a finding. Gas used was 25320 of the 300000 limit (8.4%). The same call at nonce 53 succeeded using 119837 gas with a limit of 300000 [E12].

**Next steps**
For a developer: check the current price, then send the transaction again with a new deadline and a fee high enough to be included in time [E7].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://eth.blockscout.com/tx/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a)
- **E2** (fee): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a)
- **E3** (revert): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a)
- **E4** (call): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a), [Explorer API: contract ABI](https://eth.blockscout.com/api/v2/smart-contracts/0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D)
- **E5** (source): [Repository Uniswap/v2-periphery@ed24991](https://github.com/Uniswap/v2-periphery/blob/ed24991304291297c3b4a52818d02f46a17aa9a2/contracts/UniswapV2Router02.sol#L252-L266), [Explorer API: verified source](https://eth.blockscout.com/api/v2/smart-contracts/0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D)
- **E6** (code): [Explorer API: verified source](https://eth.blockscout.com/api/v2/smart-contracts/0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D)
- **E7** (diagnosis): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a), [Source of the rule's meaning](https://github.com/Uniswap/v2-periphery/blob/ed24991304291297c3b4a52818d02f46a17aa9a2/contracts/UniswapV2Router02.sol#L19)
- **E8** (source): [Repository Uniswap/v2-periphery@ed24991](https://github.com/Uniswap/v2-periphery/blob/ed24991304291297c3b4a52818d02f46a17aa9a2/contracts/UniswapV2Router02.sol#L19-L19), [Explorer API: verified source](https://eth.blockscout.com/api/v2/smart-contracts/0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D)
- **E9** (cross_check): [JSON-RPC](https://ethereum-rpc.publicnode.com), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a)
- **E11** (timeline): [Explorer API: the sender's transactions](https://eth.blockscout.com/api/v2/addresses/0x79D6C25dE86bC4A858d7e383A474667aB109FeD0/transactions?filter=from)
- **E12** (gas_note): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x73c4c0385483897a8c3cca6e4573c880dab32265bbacd94923fff2466cb45c8a), [Explorer API: the sender's transactions](https://eth.blockscout.com/api/v2/addresses/0x79D6C25dE86bC4A858d7e383A474667aB109FeD0/transactions?filter=from)

</details>

</details>

<details><summary><strong>4. A contract the explorer has not verified (degradation), with a clarifying question (triage)</strong></summary>

Network `ethereum-mainnet`, transaction `0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9`, mode `support`.

**Assistant asks:** What were you trying to do? The failure's words are those of a swap's minimum-output (slippage) check, but this contract's own code decides what they mean here.

- Swap or trade tokens
- Something else

**Reader answers:** Something else

**Assistant:**

**O que aconteceu**

A operação falhou e não foi concluída [E1]. A causa: não dá para saber com certeza (UNKNOWN, "não dá para saber"). A possibilidade levantada é que a operação foi recusada por um limite de preço, mas isso é só uma hipótese, e quem decide o que a regra significa é o código do contrato, que não aparece nos fatos [E17]. (Motivo informado: 'INSUFFICIENT_OUTPUT_AMOUNT'; foram usadas 133145 unidades de taxa, de um limite de 600000; contrato chamado: 0x278d858f05b94576C1E6f73285886876ff6eF8D2 [E1][E17].)

**O que você nos disse**

Você disse que não estava trocando nem negociando moedas. Isso não combina bem com a hipótese do limite de preço, porque essas palavras são as de uma troca. Aqui, o significado real está no código do contrato, que não temos [E23].

**O dinheiro se mexeu?**

Não. Nada foi transferido, porque a transação falhou [E1].

**Taxa**

Foi cobrada uma taxa de rede de 0.000294067451118215 ETH, mesmo com a falha [E2].

**O que aconteceu depois**

A mesma operação deu certo cerca de 372 segundos depois, e foi a primeira vez que deu certo após esta falha [E20]. Antes disso, ela tinha falhado 2 vezes seguidas, sem outra transação no meio [E21].

**O que fazer**

Não há nada a refazer: a mesma operação já deu certo depois. Você pode conferir essa transação posterior, a de número 10346 do remetente [E20].

**O que falta saber**

Não dá para saber o que a operação fazia por dentro. Falta um contrato verificado no explorador, ou o manual técnico (ABI) do contrato, para entender a chamada [E4]. Sem isso, a causa da falha continua desconhecida [E17].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://eth.blockscout.com/tx/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9)
- **E2** (fee): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9)
- **E4** (call): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9)
- **E6** (internal_call): [Explorer API: internal transactions](https://eth.blockscout.com/api/v2/transactions/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9/internal-transactions)
- **E17** (diagnosis): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x68098e2a69deadd1f60396d667781517a5cfdf257a5e35f79f0c47c52f5f51c9), [Source of the rule's meaning](https://github.com/Uniswap/v2-periphery/blob/ed24991304291297c3b4a52818d02f46a17aa9a2/contracts/UniswapV2Router02.sol#L232)
- **E20** (timeline): [Explorer API: the sender's transactions](https://eth.blockscout.com/api/v2/addresses/0xd05E8724aD399Bb618497b1bf66a4A24Ab156351/transactions?filter=from)
- **E21** (timeline): [Explorer API: the sender's transactions](https://eth.blockscout.com/api/v2/addresses/0xd05E8724aD399Bb618497b1bf66a4A24Ab156351/transactions?filter=from)
- **E23** (triage): The reader's answer to the clarifying question

</details>

**Missing data the answer declares:**

- Call decoding: no ABI for 0x278d858f05b94576C1E6f73285886876ff6eF8D2 matches selector 0x70521ae9

</details>

<details><summary><strong>5. Triage: it succeeded, but the reader says the payment did not arrive</strong></summary>

Network `ethereum-mainnet`, transaction `0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952`, mode `support`.

**Reader:** não recebi o pagamento

**Assistant asks:** Which payment did you expect? Pick the one below, or type the address that should have received it.

- Token transfer: 69.3484 USDC from 0xCA62C34d54b445283121905F19193416212117c4 to 0x74AA5387681505c806FF1E972B12cdfd01406828.

**Reader answers:** 0x000000000000000000000000000000000000dEaD

**Assistant:**

**O que aconteceu**

A transação deu certo. Ela não falhou [E1]. A rede também confirmou isso por uma segunda verificação independente [E7]. Foi um envio de USDC feito em 7 de outubro, às 15h47 UTC, sem envio de ETH junto (valor 0 ETH) [E1].

**O que você nos disse**

Você disse que esperava um pagamento para o endereço 0x000000000000000000000000000000000000dEaD. Pelo que a lista do explorador mostra, nenhuma movimentação de valor desta transação foi para esse endereço [E9]. O dinheiro foi para 0x74AA5387681505c806FF1E972B12cdfd01406828 [E9]. Isso é só uma possibilidade, não algo provado, porque o explorador esconde alguns tokens, como os que ele marca como golpe, e eles não aparecem nessa lista [E9].

**O dinheiro se movimentou?**

Sim. Foram enviados 69.3484 USDC do endereço 0xCA62C34d54b445283121905F19193416212117c4 para o endereço 0x74AA5387681505c806FF1E972B12cdfd01406828 [E5]. Se você esperava receber o pagamento, vale conferir se esse segundo endereço é o seu. Pelos dados que tenho, o pagamento não foi para o endereço que você citou [E9].

**Taxa**

A taxa paga pela rede foi de cerca de 0,000085 ETH [E2].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://eth.blockscout.com/tx/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952)
- **E2** (fee): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952)
- **E5** (token_transfer): [Explorer API: token transfers](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952/token-transfers)
- **E7** (cross_check): [JSON-RPC](https://ethereum-rpc.publicnode.com), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952)
- **E9** (triage): The reader's answer to the clarifying question, [Explorer API: token transfers](https://eth.blockscout.com/api/v2/transactions/0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952/token-transfers)

</details>

</details>

<details><summary><strong>6. The private demo network: a BRLC call decoded from the configured repository, and the cause the explorer did not give recovered by replaying the call on the node, developer mode</strong></summary>

Network `anychain-devnet`, transaction `0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f`, mode `developer`.

**Assistant:**

**Resumo**

A transação falhou (reverteu) no bloco 63, em 2026-10-09T01:41:02.000000Z, enviada por 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC para o contrato 0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512 (BRL Coin) [E1]. O recibo obtido via RPC confirma de forma independente o status "failed", igual ao do explorer [E7].

**Taxa**

A taxa paga foi de 0.000000007752902729 ETH, e ela é cobrada mesmo com a transação falhando [E2].

**Chamada decodificada**

- Função: `setPauser(address)` no contrato 0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512 (BRL Coin) [E3].
- Argumento: `newPauser` = 0x3C44CDDdb6A900fA2b585D8C4E1a7C9ee5f0f9C3 [E3].
- Origem do ABI: repositório cloudwallk/brlc-token@74a5498, BRLCToken, fixado a este endereço na configuração [E3].

**Causa**

- UNKNOWN: o explorer não informa o motivo da falha; a causa original não é conhecida [E4].
- LIKELY: a causa é provável, não comprovada. Um replay em eth_call no bloco 62 reverteu com a razão 'Ownable: caller is not the owner', ou seja, uma verificação de acesso recusou o chamador. Se a transação original falhou do mesmo modo, essa é a causa [E6]. O replay usa o estado do bloco anterior e não inclui as transações anteriores do mesmo bloco, então pode diferir do que ocorreu de fato [E5].
- Nenhuma linha de código é apontada por um fato, então não cito nenhuma.

**Gas**

Como heurística, e não como achado: foram usados 28963 de um limite de 200000 (14.5%) [E9].

**Próximos passos**

Para um desenvolvedor:
- Encontre onde reverteu com um nó que consiga rastrear a transação, ou pergunte aos desenvolvedores do contrato [E4].
- Reenviar a mesma chamada sem alterações deve falhar do mesmo jeito e cobrar a taxa de novo, pois a recusa vem de uma verificação de permissão [E6].
- Descubra qual conta a verificação recusou e se ela deveria ter a permissão; se deveria, peça aos operadores do contrato que a concedam [E6].

**O que falta**

- Motivo da reversão na transação original: o explorer não informou. É necessário um nó que reexecute ou trace a chamada (RPC de debug/trace). Não é recuperável por nova tentativa [E4].
- Chamadas internas: o explorer ainda as está indexando. Consulte novamente em alguns minutos [E8].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](http://127.0.0.1:14000/tx/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f), [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f)
- **E2** (fee): [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f)
- **E3** (call): [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f), [Repository cloudwallk/brlc-token@74a5498](https://github.com/cloudwallk/brlc-token/blob/74a5498d04b10dc882e48273a38b98b4275463bd/contracts/BRLCToken.sol#L12-L60)
- **E4** (diagnosis): [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f)
- **E5** (replay): [JSON-RPC](http://127.0.0.1:18545)
- **E6** (diagnosis): [JSON-RPC](http://127.0.0.1:18545), [Source of the rule's meaning](https://github.com/OpenZeppelin/openzeppelin-contracts/blob/dc44c9f1a4c3b10af99492eed84f83ed244203f6/contracts/access/Ownable.sol#L51)
- **E7** (cross_check): [JSON-RPC](http://127.0.0.1:18545), [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f)
- **E8** (timeline): [Explorer API: the sender's transactions](http://127.0.0.1:14000/api/v2/addresses/0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC/transactions?filter=from)
- **E9** (gas_note): [Explorer API: transaction](http://127.0.0.1:14000/api/v2/transactions/0x42864ecb1fdcb0f155cfb0d9c085452fdcfa21170cdf44c7f62bfafb5aed027f)

</details>

**Missing data the answer declares:**

- Revert reason: the explorer did not report why the transaction failed
- Internal calls: the explorer is still indexing this transaction's internal calls

</details>

<details><summary><strong>7. An auditor's review, in English: a security note on the called function's code (heuristic, not an audit)</strong></summary>

Network `ethereum-mainnet`, transaction `0x4eb27f6b86c81685f8c23b2abcaf5cc9510ed1f2a737c42d5ff344914f890dd0`, mode `auditor`, answer language `en`.

**Assistant:**

**Summary**
The transaction succeeded in block 26148933, sent from 0xf443b891Ad0c3910AD430c7D619cf9a20B3a00b2 to 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45 (SwapRouter02) with 0 ETH of native value [E1]. An RPC receipt independently reports the same success status as the explorer [E23].

**Fee**
The fee paid was 0.000440126081949696 ETH [E2].

**Behavior and permissions**
The shown code of `multicall(bytes[])` is declared `public payable override` and returns a `bytes[]` array [E4]. It creates a results array with the same length as the input data [E4]. For each item it runs `address(this).delegatecall(data[i])` [E4]. If a call fails and the returned data is shorter than 68 bytes, it reverts with no message [E4]. Otherwise it reverts with the decoded error string [E4]. If a call succeeds, its result is stored in the results array [E4]. The shown lines contain no modifier by name that guards the function, apart from `payable` and `override` in the declaration [E4]. Whether other access checks exist in the called code is not shown in these lines [E4].

**Security notes**
The note is a heuristic pattern match on the source, not an audit and not a finding that the contract is vulnerable. The helpers the function calls were not read [E5]. Line 14 uses delegatecall to this contract's own code in a loop (the multicall pattern). Every item sees the same msg.value, and the function is payable. This is the setting of msg.value reuse bugs, where several items each count the same payment [E5]. The absence of other notes does not mean the code is safe [E5].

For a developer: review by hand whether each item reachable through this loop accounts for msg.value separately, and read the helper functions that the note says were not read [E5].

<details><summary>Sources of the facts it cites</summary>

- **E1** (overview): [Explorer page](https://eth.blockscout.com/tx/0x4eb27f6b86c81685f8c23b2abcaf5cc9510ed1f2a737c42d5ff344914f890dd0), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x4eb27f6b86c81685f8c23b2abcaf5cc9510ed1f2a737c42d5ff344914f890dd0)
- **E2** (fee): [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x4eb27f6b86c81685f8c23b2abcaf5cc9510ed1f2a737c42d5ff344914f890dd0)
- **E4** (code): [Explorer API: verified source](https://eth.blockscout.com/api/v2/smart-contracts/0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45)
- **E5** (security_note): [Explorer API: verified source](https://eth.blockscout.com/api/v2/smart-contracts/0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45)
- **E23** (cross_check): [JSON-RPC](https://ethereum-rpc.publicnode.com), [Explorer API: transaction](https://eth.blockscout.com/api/v2/transactions/0x4eb27f6b86c81685f8c23b2abcaf5cc9510ed1f2a737c42d5ff344914f890dd0)

</details>

</details>

---

## 5. Evaluation results

`anychain eval` replays 11 real transactions from their recordings (8 on Ethereum, 2 on Optimism, 1 on the private demo network; `eval/cases.yaml`) through the whole pipeline, the model included, and measures the answer. Latest run ([eval/report.md](eval/report.md), per-case details in `eval/report.json`):

Run 2026-10-09 11:37, commit 7f39aba (public id af9391b), model claude-sonnet-5-5. Every case is a real transaction replayed from its recording (eval/cases.yaml).

| Metric | Result |
|---|---|
| Status accuracy | 100.0% |
| Diagnosis rule and label accuracy | 100.0% |
| ABI source accuracy | 100.0% |
| Degradation declared correctly | 100.0% |
| Citation coverage (factual sentences citing a fact) | 85.1% |
| First drafts the check caught (a value or citation not in the evidence; rewritten) | 0.0% |
| Key values present in the answer | 100.0% |
| Key facts cited (each conclusion, and what the sender's timeline shows) | 100.0% |
| Delivered answers with a value not in the evidence | 0 (every answer shown passed the check) |
| Values the check caught on a first attempt | 0 |
| Answers withheld | 0 of 11 |
| Time per written answer | 12.2 s |
| Tokens sent (cache included) / received | 59248 / 10964 (12981 read from the cache) |
| Cost reported by the backend | 0.2973 USD |

<details><summary>Per case (11 real transactions)</summary>

| Case | Network | Status | Rule / label | ABI | Gaps | Answer | Cited | Values | Key facts | Seconds | Tokens in/out |
|---|---|---|---|---|---|---|---|---|---|---|---|
| erc20_transfer | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 11.4 | 3874/358 |
| dex_swap | ethereum-mainnet | ok success | ok -  | ok explorer |  - | ok | 22/22 | 2/2 | 0/0 | 14.2 | 5554/2009 |
| explicit_reason | ethereum-mainnet | ok failed | ok deadline CONFIRMED | ok explorer |  - | ok | 20/22 | 2/2 | 2/2 | 10.5 | 5280/1458 |
| inner_out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas LIKELY | ok explorer |  - | ok | 11/11 | 2/2 | 2/2 | 22.6 | 6854/578 |
| out_of_gas | ethereum-mainnet | ok failed | ok out_of_gas CONFIRMED | ok explorer |  not_interpretable | ok | 10/13 | 1/1 | 2/2 | 7.7 | 4682/627 |
| unverified_contract | ethereum-mainnet | ok failed | ok slippage LIKELY | ok raw |  not_interpretable | ok | 29/33 | 1/1 | 3/3 | 24.0 | 6270/1891 |
| custom_error | ethereum-mainnet | ok failed | ok contract_reason CONFIRMED | ok raw |  not_interpretable | ok | 25/31 | 1/1 | 2/2 | 15.6 | 9623/1997 |
| node_unavailable | ethereum-mainnet | ok success | ok -  | ok explorer | ok source_unavailable | ok | 5/8 | 1/1 | 0/0 | 6.6 | 3822/424 |
| second_network_transfer | optimism-mainnet | ok success | ok -  | ok explorer |  - | ok | 4/4 | 2/2 | 0/0 | 4.2 | 3940/261 |
| second_network_failure | optimism-mainnet | ok failed | ok deadline LIKELY | ok signature_db |  not_interpretable | ok | 11/16 | 1/1 | 2/2 | 8.6 | 4838/620 |
| allowance_devnet | anychain-devnet | ok failed | ok no_reason UNKNOWN (replay: insufficient_allowance) | ok raw | ok not_interpretable, source_behind | ok | 13/17 | 1/1 | 2/2 | 8.6 | 4511/741 |

</details>

The commit ids quoted in reports and decisions are those of the development history; their public ids are in [docs/COMMIT_MAP.md](docs/COMMIT_MAP.md) (the history was rewritten once to remove personal data). Citation coverage varies between runs while the other metrics hold: 78.1% to 85.1% over the four runs of 2026-10-09 with the final prompts; the model writes differently each time. A fresh clone reproduces every metric except the written answer's (the facts-only run, `anychain eval --no-llm`, needs no model and gives the same accuracy columns).

The allowance category had no real case on a public network (the candidate found was an inner out-of-gas, D50, D51); its real case comes from the private demo network, where the explorer gives no reason and the replay on the node finds "insufficient allowance" (D60). "First drafts the check caught" counts answers the model had to rewrite because they stated a value or cited a fact not in the evidence: none in this run. Earlier runs showed one ("E624"), which turned out to be the check's mistake, not the model's: a shortened address ("0x52b2…E624") read as a citation; fixed (D62).

**Smaller models:** the same eval with gpt-4.1-nano (OpenAI's smallest model), on the same commit and prompts (`eval/runs/nano-final-d67`): the same diagnosis, 100% of the key values and key facts, no answer withheld, 80.3% of factual sentences citing a fact. Getting there took code, not a longer prompt: an outline built for each transaction from its facts, only the reader's own next steps, citations normalized, and the key facts enforced (D62, with each step measured). The merchant's plain language (D67) lets more jargon through with the small model than with a larger one.

**Acceptance against an independent source:** 1,800 randomly sampled transactions (300 on each of six networks), checked against the network's node: six checks each (status, value, fee, token transfers, no double-counted native value, addresses exist) plus the L1 status on zkSync, 11,100 checks in all: 11,008 passed, 88 had no node data to compare, 4 flagged. The 3 false facts found (a missing receipt called "pending", a blob fee left out on Gnosis) were fixed with tests on the recorded cases; the fourth is a token the explorer hides as scam ([docs/acceptance-report.md](docs/acceptance-report.md)). The run used the Phase 3 code: the Phase 4 facts (timeline, gas notes, triage) come from the same explorer lists and are covered by tests on recordings, not by this run.

---

## 6. Metrics and measuring impact in production

`anychain metrics` prints usage numbers from the local event log (`data/runs.db`, one row per answer given from the CLI or the API; it fills as the tool is used, so a fresh clone shows empty tables), each with the SQL that computes it (all five below; the same text is in `src/anychain/metrics.py`):

<details><summary>The five queries</summary>

```sql
-- Failure causes (the diagnosis rule of each failed transaction)
SELECT COALESCE(rule, 'no conclusion') AS cause, COUNT(*) AS answers,
       COUNT(DISTINCT network || tx_hash) AS transactions, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND status = 'failed'
GROUP BY cause ORDER BY answers DESC;

-- Diagnosis labels (CONFIRMED / LIKELY / UNKNOWN)
SELECT COALESCE(diagnosis, 'unlabelled') AS label, COUNT(*) AS answers,
       COUNT(DISTINCT network || tx_hash) AS transactions, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND status = 'failed'
GROUP BY label ORDER BY answers DESC;

-- ABI source of the main call (explorer / repo / signature-database candidates / raw)
SELECT abi_source, COUNT(*) AS answers, COUNT(DISTINCT network || tx_hash) AS transactions,
       100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND abi_source IS NOT NULL
GROUP BY abi_source ORDER BY answers DESC;

-- Cache (answers served without asking the explorer and the node)
SELECT CASE WHEN cache = 'hit' THEN 'hit' WHEN cache = 'stored' THEN 'stored'
            WHEN cache LIKE 'not kept%' THEN 'not kept' ELSE COALESCE(cache, 'unknown') END AS cache,
       COUNT(*) AS answers, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL
GROUP BY 1 ORDER BY answers DESC;

-- Satisfaction by mode (thumbs up and down from the page)
SELECT mode, COALESCE(SUM(feedback = 'up'), 0) AS up, COALESCE(SUM(feedback = 'down'), 0) AS down,
       100.0 * SUM(feedback = 'up') / NULLIF(SUM(feedback IS NOT NULL), 0) AS satisfaction
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL
GROUP BY mode ORDER BY mode;
```

</details>

### Measuring impact in production (summary of [docs/IMPACT.md](docs/IMPACT.md))

- **Where:** in the support tool, next to a ticket that carries a transaction; the support agent reads the answer first and decides what to send; escalation hands engineering the developer view of the same evidence.
- **Hypothesis:** for transaction tickets, support resolves more without escalating to engineering, faster, without more wrong answers sent. Target to test: a 30% relative drop in escalations (an assumption, measured against a baseline).
- **Primary metrics:** resolution without escalation; time from ticket open to the first message stating the cause. **Guard metrics:** answers corrected by humans (edited cause, 👎, ticket reopened), reported hallucinations (target zero, each one becomes a test), answers withheld or without a model. **Health:** gaps by cause per network, cost, latency and cache per answer.
- **Experiment:** two weeks in shadow (answers recorded, never shown) for the baseline and the guards, then randomized by ticket at 10%, 25%, 50%, 100%, each stage until the primary metric's interval is clear.
- **Unit economics:** a written answer costs about US$ 0.027 (measured, Claude Sonnet). With assumed costs to replace with real ones (support at US$ 15 per hour, an escalation at US$ 30 of engineering time), an answer pays for itself if it saves about 6.5 seconds of an agent, and one avoided escalation pays for about 1,100 answers; the decision rests on the guard metrics, not on the model's cost.
- **Scale or roll back:** next stage when escalations drop and every guard holds; roll back on any confirmed hallucination sent to a customer, corrections above the control's rate, or more than 10% withheld or unavailable answers in a day. A rollback is a flag: recording continues.

---

## 7. Known limits and next steps

- **Public infrastructure:** public nodes keep the state of about the last 128 blocks, so reads at the parent block of an older failure are refused (said as a gap); one public node returns some old transactions without a receipt (the outcome is then "unknown", D53). An archive node removes both.
- **No trace yet:** where the explorer's internal calls do not show the failing frame, `debug_traceTransaction` would. The config records whether a node serves it (`rpc.supports_debug_trace`; Stratus does), but the tool does not call it yet: failures are explained from the explorer, reads on the node and a replay.
- **Explorer-hidden tokens:** a token the explorer marks as scam has its transfers hidden; the tool does not say so yet (backlog C1).
- **Heuristics stay heuristics:** security and gas notes are pattern matches on the shown code, never an audit.
- **Private demo network:** built and recorded (devnet/), but on a private server and rebuilt by hand; a CI job that brings it up, sends the transactions and runs `anychain eval` against it would keep the retargeting proof current. Calls inherited from OpenZeppelin are not decoded there, because the library is not in the configured repository: adding its source as a second repository would decode them.
- **Backlog:** imprecise wordings found by reviews and acceptance runs (level C), each with the real transaction it came from, in docs/ROADMAP.md.

---

## 8. How AI was used to build this

The code, tests and documents were written with Claude Code, in phases (Phase 1 from the plan in the case itself; Phases 2, 2.5, 3 and 4 each from a written spec approved before any code, in `docs/specs/`). The human decisions, recorded in `docs/DECISIONS.md` with dates, were mine: the scope and the order of the phases; the acceptance criterion (zero false facts of level A or B on 300 sampled transactions per network, checked against the node); which imprecise wordings to fix now and which to keep for later; running the writer on Claude Code instead of an API key; the retargeting target (CloudWalk) and its sources; and, after the evaluation found a rule that ignored an inner out-of-gas, asking for the diagnosis to start from the failure's origin. The rules the model worked under are in [CLAUDE.md](CLAUDE.md) (in Portuguese, as written for it; they came from real mistakes during the project), enforced by two hooks in `.claude/settings.json`: the tests run after every edit, and a commit is refused unless the whole suite passes. The process the model followed: a failing test before each fix, written from a real recorded transaction (nothing in the tests is invented); a review by a separate agent with a clean context before commits; and an independent acceptance run against the network's own node at the end of each phase.
