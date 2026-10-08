# Architecture

Living document: updated at the end of every phase. Colors show what exists. A Portuguese version is kept in `ARCHITECTURE.pt-BR.md`.

- **Green**: built and tested
- **Yellow**: next phase
- **Grey**: later phases
- White boxes are only groupings.

View the diagrams in VS Code (extension "Markdown Preview Mermaid Support") or on GitHub, which renders them natively.

Current state: **end of phase 1** (2026-10-07).

---

## 1. The pipeline

One rule shapes everything: facts are collected **without any AI**, numbered, and given a source. The AI only writes prose from those facts, and (from phase 2) a validator checks it did not invent anything.

```mermaid
flowchart TD
    IN["Input<br/>tx hash + mode + question"] --> CFG

    CFG["Config (YAML)<br/>explorer, RPC, repos, LLM model,<br/>chain_type, address labels<br/>one file per network"]

    CFG --> COL
    subgraph COL["1. Collectors"]
        EXP["Explorer client<br/>Blockscout API v2"]
        RPC["RPC client<br/>JSON-RPC node"]
        REPO["Repo client<br/>GitHub clone + index"]
    end

    COL --> DEC["2. Decoder<br/>calldata, events, anonymous events<br/>ABI from explorer"]
    DEC --> PRO["Chain-type profile<br/>default, ethereum, optimism,<br/>optimism-celo, rsk, zksync<br/>fees, L1 status, deposits"]
    PRO --> BUN
    DEC --> CAS["ABI cascade<br/>repo artifacts, source signatures,<br/>4byte, raw"]
    DEC --> DIAG["3. Diagnostics<br/>only if failed: revert reason,<br/>eth_call state reads, rules"]
    DEC --> BUN
    CAS --> BUN
    DIAG --> BUN

    BUN["4. Evidence bundle<br/>facts E1, E2... each with a source<br/>gaps: what is missing + retryable?"]
    BUN --> WRI["5. LLM writer<br/>prompt per mode, cites [E#]"]
    BUN --> REN["Deterministic render<br/>works with no LLM"]
    WRI --> VAL["6. Validator<br/>cited ids exist, no invented<br/>addresses, values or hashes"]
    VAL --> OUT
    REN --> OUT

    OUT["7. Output<br/>markdown + JSON"] --> CLI["CLI<br/>anychain explain"]
    OUT --> API["API + web UI<br/>chat with tools"]
    OUT --> DB["SQLite runs<br/>metrics + eval"]
    BUN --> LOG["Event log<br/>one row per answer:<br/>gaps with cause, crashes,<br/>checks against the node"]
    LOG --> QRY["anychain log<br/>query by network and cause"]
    LOG -.production, design only.-> FILA["Queue (e.g. SQS)"]
    FILA -.-> WRK["Worker<br/>aggregates by network and cause,<br/>alerts"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef next fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64

    class IN,CFG,EXP,RPC,DEC,PRO,BUN,WRI,REN,OUT,CLI,LOG,QRY done
    class REPO,CAS,DIAG,VAL next
    class API,DB,FILA,WRK later
    style COL fill:#ffffff,stroke:#90a4ae,color:#263238
```

| Phase | Adds |
|---|---|
| 1 (done) | config, explorer + RPC collectors, decoder with explorer ABI, evidence bundle, LLM writer, CLI |
| 2 | diagnostics for failures, `eth_call` state reads, repo grounding, full ABI cascade, confidence levels, validator |
| 3 | API, web UI, chat with tools, modes, SQLite + metrics, eval suite |
| 4 | triage questions, multi-transaction timeline, security notes, gas notes |

---

## 2. What happens on one `explain` call

Retries, the time budget and the fallbacks, in order. Every failure becomes a **gap** instead of an error on screen.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant C as CLI
    participant B as Bundle builder
    participant E as Explorer (Blockscout)
    participant R as RPC node
    participant L as LLM (Claude)

    U->>C: anychain explain 0x... --config eth.yaml
    C->>B: build(hash), time budget 30s
    B->>E: GET /transactions/{hash}
    Note over B,E: network trouble: up to 3 attempts<br/>404 or bad request: no retry
    E-->>B: transaction (or gap)
    B->>R: eth_chainId (right network?)
    B->>R: eth_getTransactionByHash + receipt
    R-->>B: independent copy (or gap)
    alt explorer answered
        par in parallel, each fetched once (D26)
            B->>E: token transfers, internal calls, logs
        and
            B->>E: contract ABIs of the call target and log emitters<br/>(proxy -> implementation, or EIP-7702 delegate)
        end
        Note over B: target without code today?<br/>only "no code" if this tx cleared its delegation,<br/>otherwise "cannot confirm" + gap
        Note over B: each step isolated:<br/>one broken part = one gap
    else explorer down or lagging
        Note over B: RPC-only summary,<br/>gap says what is missing
    end
    B-->>C: evidence bundle (facts + sources + gaps)
    opt LLM available and not --no-llm
        C->>L: only the facts, explorer links and gaps<br/>(no API or node addresses)
        L-->>C: explanation citing [E1], [E3]...
    end
    C-->>U: explanation + evidence list + missing data
```

---

## 3. Code map

Which file does what, and who calls whom.

```mermaid
flowchart LR
    subgraph entry["Entry points"]
        cli["cli.py<br/>anychain explain<br/>anychain log"]
    end

    subgraph core["Core"]
        config["config.py<br/>load + validate YAML"]
        chains["chains.py<br/>one profile per chain type"]
        bundle["bundle.py<br/>builds the evidence"]
        decoder["decoder.py<br/>ABI decoding"]
        models["models.py<br/>Evidence, Gap (with cause), Bundle"]
        events["events.py<br/>event log: RunEvent,<br/>SqliteEventLog, queries"]
    end

    subgraph collectors["collectors/"]
        http["http.py<br/>retries, budget,<br/>retryable errors"]
        types["types.py<br/>typed objects: Transaction,<br/>InternalCall, TokenTransfer, Log..."]
        explorer["explorer.py<br/>Blockscout"]
        rpc["rpc.py<br/>JSON-RPC"]
        replay["replay.py<br/>record / replay<br/>real traffic for tests"]
    end

    subgraph output["Output"]
        render["render.py<br/>markdown, no LLM"]
        writer["writer.py<br/>LLM call"]
        prompts["prompts/*.md<br/>one per mode"]
    end

    cli --> config
    cli --> bundle
    cli --> render
    cli --> writer
    cli --> events
    bundle --> explorer
    bundle --> rpc
    bundle --> decoder
    bundle --> models
    bundle --> chains
    explorer --> http
    rpc --> http
    explorer --> types
    rpc --> types
    bundle --> types
    writer --> prompts
    replay -.used by tests.-> explorer
    replay -.used by tests.-> rpc
```

---

## 4. How a gap is classified

The `retryable` flag is what would let production send only network failures to a retry queue (see DECISIONS D11).

```mermaid
flowchart TD
    F["A source failed"] --> Q1{"What kind of failure?"}
    Q1 -->|"timeout, dropped connection,<br/>429, 5xx, HTML page"| N["Network trouble"]
    Q1 -->|"404, other 4xx,<br/>invalid JSON-RPC params"| RQ["Request problem"]
    Q1 -->|"node says busy<br/>(JSON-RPC -32005)"| N
    N --> RT["Retry now: up to 3 attempts<br/>within the time budget"]
    RT -->|still failing| G1["Gap, retryable = true<br/>'try again in a few minutes'"]
    RT -->|recovered| OK["Fact added"]
    RQ --> G2["Gap, retryable = false<br/>says what is needed instead"]

    classDef gap fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef ok fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class G1,G2 gap
    class OK ok
```

Besides "retryable", every gap carries a **cause**, which the event log counts (D24). Every gap site must name one:

| Cause | Meaning | Problem? |
|---|---|---|
| `source_unavailable` | a source did not answer (timeout, 5xx, rate limit) | yes, alert when the rate rises |
| `source_error` | a source refused or sent something unreadable (4xx, node error, missing field) | yes |
| `source_behind` | the explorer has not indexed it yet, or disagrees with the node | yes, if it persists |
| `processing_error` | our code failed on the payload, or our arithmetic contradicts a source | yes, always a defect |
| `config_error` | the network config does not fit the sources (wrong chain id or chain type) | yes |
| `pending` | the transaction is not mined yet | no |
| `not_interpretable` | no ABI, list truncated, unknown field, hash not found | no |

---

## 5. Event log and alerts

Today the log is a local SQLite file queried with `anychain log`. In production the same event would go to a queue and a worker would aggregate and alert; that part is design only.

```mermaid
flowchart LR
    EX["anychain explain<br/>(later the API)"] --> EV["RunEvent<br/>network, hash, duration,<br/>gaps with cause, crash"]
    CAN["Canary: acceptance script<br/>(today run by hand; daily schedule<br/>is production design)"] --> EV
    EV --> SINK{"storage.event_sink"}
    SINK -->|sqlite| DBL["data/runs.db"]
    DBL --> Q["anychain log<br/>totals by network and cause,<br/>--problems lists the cases"]
    SINK -.->|"queue (production)"| QUEUE["Queue"]
    QUEUE -.-> W["Worker"]
    W -.-> AL["Alert<br/>crash or processing error: always<br/>source unavailable above 5% per hour<br/>a canary check failed"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64
    class EX,EV,CAN,SINK,DBL,Q done
    class QUEUE,W,AL later
```

The canary is the only way to catch the silent kind (a false fact nothing flagged), which is why the acceptance run writes to the same log with source `canary`.

---

## Changelog of this document

- **2026-10-07, end of phase 1:** first version. Pipeline, call sequence, code map, gap classification.
- **2026-10-08, level C fixes (D26):** explorer requests in parallel and fetched once; the internal-call cutoff keeps every call that moved value; reverts worded as the explorer reports them.
- **2026-10-07, zkSync L1 status (D25):** the explorer's status is never asserted; the profile confirms it with the node (`node_facts`); every gap must name its cause, with the new cause `config_error`.
- **2026-10-07, event log (D24):** every gap has a cause; every answer becomes an event in local SQLite, queried with `anychain log`; production design with a queue and a worker (not built).
- **2026-10-07, typed objects (D21):** explorer and RPC answers become typed objects once, in `collectors/types.py`.
- **2026-10-07, chain-type profiles (D20):** config names the Blockscout CHAIN_TYPE; profiles for six network types; address labels in config.
- **2026-10-07, phase 1 review rounds:** EIP-7702 delegates as an ABI source; accounts without code today are never asserted codeless (D18).
