# Using AnyChain: the page, the CLI and the API

## The page

**The page** (`anychain serve`, then http://127.0.0.1:8000) is a conversation, in three versions chosen on its first screen:

- **Merchant** (`#/lojista`, also `#/merchant`): plain words, the answer at a glance (worked or not, value moved, the fee rounded, the date in the reader's time zone), what to do now; the proof slides in on demand.
- **Developer** (`#/desenvolvedor`, also `#/developer`) and **Auditor** (`#/auditor`): the facts and their sources always beside the thread; the auditor sees the security notes first. A "facts only, no AI" switch.

The reader writes as they like ("meu pagamento 0x… não passou"): a transaction hash or an address anywhere in the message starts an explanation, the rest of the message is the question; an address lists its transactions as buttons; the clarifying question, when there is one, comes as a message with buttons. Any other message is a follow-up on the same transaction, answered by the chat with tools (a read on the node, a function's code, another transaction), each result a new sourced fact. Every citation `[E#]` opens its fact. Each network's config offers real example transactions.

**Settings** (⚙): the **network** (any of `configs/`, switched without restarting), **add a network** (chain id, coin, Blockscout explorer, node; "Test the connection" checks that the node answers on that chain id; saved on this computer only, in `~/.config/anychain/networks/`, mode 0600, because a node's address can carry a key; it is never sent back to the page), the **model** (Anthropic, OpenAI or local Claude Code; the key is saved on this computer and never shown again; the model is picked from the provider's own list for that key) and the **language** of the answers and the page (pt-BR, en, es). The settings endpoints answer only requests from the page itself (a custom header and an Origin check).

## Commands and API

**Interfaces:** `anychain explain <hash> [--mode support|developer|auditor] [--config path] [--json] [--evidence] [--question "…"] [--no-llm] [--fresh]`, `anychain chat`, `anychain batch <file>`, `anychain serve`, `anychain eval`, `anychain metrics`, `anychain log`, `anychain repos sync`, `anychain llm show|set|test|clear`. API: `POST /explain` (hash, mode, question?, clarified?), `POST /chat` (session_id), `POST /feedback`, `GET /health` (active network; explorer, node and model status), `GET/POST /settings/llm`, `POST /settings/llm/models` (the provider's list for a key) and `POST /settings/llm/test` (the model and its key; the key is never returned), `GET/POST /settings/network` (list and switch), `GET /settings/network/draft`, `POST /settings/network/check` and `POST /settings/network/save` (add a network), `GET/POST /settings/answers` (language).

## Docker

With Docker (no secrets in the image; the port is published on the host's 127.0.0.1 only):

```bash
docker build -t anychain .
docker run --rm -p 127.0.0.1:8000:8000 anychain                                   # API + page, Ethereum
docker run --rm anychain anychain explain 0x7909bd56b9a3a0e932fa20ccd7093fcafcad133c51af652c921cd329b2307952 --no-llm
```

Do not run it with `--network host`: inside the container the API listens on 0.0.0.0, which with the host's network means every interface of the host. In a container the Claude Code CLI is not available: for a written answer set `llm.provider: anthropic` (or `openai`) in a mounted config and pass the key as an environment variable at run time.

## Usage metrics

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

## Measuring impact in production (summary of [docs/IMPACT.md](IMPACT.md))

- **Where:** in the support tool, next to a ticket that carries a transaction; the support agent reads the answer first and decides what to send; escalation hands engineering the developer view of the same evidence.
- **Hypothesis:** for transaction tickets, support resolves more without escalating to engineering, faster, without more wrong answers sent. Target to test: a 30% relative drop in escalations (an assumption, measured against a baseline).
- **Primary metrics:** resolution without escalation; time from ticket open to the first message stating the cause. **Guard metrics:** answers corrected by humans (edited cause, 👎, ticket reopened), reported hallucinations (target zero, each one becomes a test), answers withheld or without a model. **Health:** gaps by cause per network, cost, latency and cache per answer.
- **Experiment:** two weeks in shadow (answers recorded, never shown) for the baseline and the guards, then randomized by ticket at 10%, 25%, 50%, 100%, each stage until the primary metric's interval is clear.
- **Unit economics:** a written answer costs about US$ 0.027 (measured, Claude Sonnet). With assumed costs to replace with real ones (support at US$ 15 per hour, an escalation at US$ 30 of engineering time), an answer pays for itself if it saves about 6.5 seconds of an agent, and one avoided escalation pays for about 1,100 answers; the decision rests on the guard metrics, not on the model's cost.
- **Scale or roll back:** next stage when escalations drop and every guard holds; roll back on any confirmed hallucination sent to a customer, corrections above the control's rate, or more than 10% withheld or unavailable answers in a day. A rollback is a flag: recording continues.
