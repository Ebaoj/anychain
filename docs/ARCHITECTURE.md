# Architecture

Living document: updated at the end of every phase. Colors show what exists. A Portuguese version is kept in `ARCHITECTURE.pt-BR.md`.

- **Green**: built and tested
- **Yellow**: being built in this phase
- **Grey**: later phases
- White boxes are only groupings.

View the diagrams in VS Code (extension "Markdown Preview Mermaid Support") or on GitHub, which renders them natively.

Current state: **end of phase 3** (2026-10-08): cache, metrics, API, chat, web page, evaluation, production impact page (`docs/IMPACT.md`), the reader's question with the hash, the recorded demo (`docs/demo/`) and the 1,800-transaction acceptance run.

---

## 0. What this is, in one minute

An assistant that **explains a blockchain transaction** to someone non-technical (a merchant: "why didn't my payment go through?") and to someone technical (a developer, an auditor), on **any EVM network**, by changing only the configuration.

The idea behind everything: **the AI finds nothing out; it only writes.** The code finds things out, and code can be tested.

```mermaid
flowchart LR
    Q["1. Question<br/>the transaction hash<br/>and who the answer is for"] --> F
    F["2. Facts, no AI<br/>explorer + the network's node<br/>each fact numbered (E1, E2...)<br/>with the source it came from"] --> D
    D["3. Conclusion, no AI<br/>diagnosis rules<br/>reads on the node as proof<br/>CONFIRMED / LIKELY / UNKNOWN"] --> W
    W["4. Prose, with AI<br/>writes only from the facts<br/>cites [E3] in every sentence"] --> C
    C["5. Check, no AI<br/>every number, address and link<br/>must be in the facts<br/>else: one retry, then withheld"] --> R["6. Answer<br/>terminal, API, web page<br/>and a chat to ask more"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class Q,F,D,W,C,R done
```

Why this way:

- **If the AI makes a mistake, the check catches it.** A number that is not in the facts never reaches the customer.
- **If a rule makes a mistake, it can be found and fixed with a test.** That happened in phase 3: the evaluation found a rule that ignored an inner call running out of gas; the rule was fixed and the real case became a test (D50, D51).
- **When data is missing, the answer says what is missing** ("the explorer did not answer; try again") instead of guessing.

---

## 1. The pipeline

```mermaid
flowchart TD
    IN["Input<br/>hash + mode (support, developer, auditor)<br/>+ optional question"] --> CACHE
    CACHE{"Answered before?<br/>cache by network and hash<br/>(final and complete transactions only)"} -->|yes| BUN
    CACHE -->|no| COL
    CFG["Configuration (YAML)<br/>explorer, node, repos,<br/>AI model, network type<br/>one file per network"] --> COL

    subgraph COL["1. Collectors"]
        EXP["Explorer<br/>Blockscout API v2"]
        RPC["Network node<br/>JSON-RPC (checks the explorer)"]
        REPO["Repos<br/>published code and ABIs"]
    end

    COL --> DEC["2. Decoder<br/>call, events, internal calls<br/>ABI cascade: explorer, repo artifacts,<br/>repo source, signature database, raw data"]
    DEC --> PRO["Network-type profile<br/>fees, L1 status, deposits"]
    DEC --> CODE["Called function's code<br/>and security notes (heuristic)"]
    DEC --> DIAG["3. Diagnosis (section 2)<br/>only when it failed"]
    PRO --> BUN
    CODE --> BUN
    DIAG --> BUN

    BUN["4. Evidence bundle<br/>facts E1, E2... with source and confidence<br/>gaps: what is missing and why"]
    BUN --> WRI["5. AI writer<br/>one prompt per mode<br/>Claude Code (default), Anthropic or OpenAI APIs"]
    WRI --> VAL["6. Check<br/>citations, numbers, addresses, links,<br/>amounts in the token's units"]
    VAL --> OUT["7. Outputs"]
    BUN --> OUT

    OUT --> CLI["Terminal<br/>explain, chat, batch"]
    OUT --> API["Local API<br/>/explain /chat /health /feedback"]
    API --> PAGE["Web page<br/>with chat and 👍/👎"]
    OUT --> LOG["Event log<br/>one row per answer"]
    LOG --> MET["anychain log and anychain metrics<br/>(each number with its SQL)"]
    EVAL["anychain eval<br/>10 real cases, measured"] -.uses.-> BUN

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class IN,CACHE,CFG,EXP,RPC,REPO,DEC,PRO,CODE,DIAG,BUN,WRI,VAL,OUT,CLI,API,PAGE,LOG,MET,EVAL done
    style COL fill:#ffffff,stroke:#90a4ae,color:#263238
```

| Phase | What it adds |
|---|---|
| 1 (done) | configuration, explorer and node collectors, decoder, evidence bundle, AI writer, terminal |
| 2 (done) | failure diagnosis, state reads, repos, ABI cascade, confidence per fact, answer check |
| 2.5 (done) | function code, security notes, ABIs from artifacts, access control, deadline parameter, labels, steps per reader, structured answer |
| 3 (done) | cache and batch, metrics, API, chat with tools, web page, amounts in the token's units, evaluation, origin-first diagnosis, the reader's question, impact page, recorded demo |
| 4 | triage (start from the problem, no hash), multi-transaction timeline, gas notes, node trace, private demo network, Dockerfile, final README |

---

## 2. How the diagnosis decides (only when the transaction failed)

Rules written in code, in the order below. Each conclusion carries a label: **CONFIRMED** (proven by the decoded reason or a read on the node), **LIKELY** (suggested by a pattern) or **UNKNOWN** (no data to conclude; the answer says what is missing).

```mermaid
flowchart TD
    S["The transaction failed"] --> O{"1. Where did it begin?<br/>did an internal call fail with an<br/>execution error (e.g. 'out of gas')?"}
    O -->|yes| OR["The origin is that call<br/>the error at the top is its consequence<br/>(e.g. TRANSFER_FROM_FAILED from out of gas inside)"]
    O -->|no| R{"2. What is the reason at the top?"}
    R -->|"balance, allowance,<br/>router could not pull the token"| READ["Read on the node, at the parent block:<br/>balance, approval, decimals<br/>less than asked? CONFIRMED"]
    R -->|"Ownable, AccessControl"| ACC["Read owner() or hasRole()<br/>confirmed only if the error names the sender"]
    R -->|"paused, slippage,<br/>deadline"| TXT["The text's meaning<br/>(deadline: the parameter compared<br/>with the block time)"]
    R -->|"out of gas"| GAS["All the gas used?<br/>the node's receipt agrees? CONFIRMED"]
    R -->|"another contract message"| OWN["The contract's own reason<br/>its meaning is in its code"]
    R -->|"no reason"| REP["Replay the call on the node<br/>(a hint only: LIKELY)"]
    OR --> CHK
    READ --> CHK
    ACC --> CHK
    TXT --> CHK
    GAS --> CHK
    OWN --> CHK
    REP --> CHK
    CHK{"3. Does the conclusion explain every signal?<br/>an inner error not cited,<br/>all gas used when it is not about gas"}
    CHK -->|yes| OUT["Conclusion with its label<br/>+ steps for a merchant and for a developer"]
    CHK -->|no| DOWN["Says the signal left over<br/>and CONFIRMED becomes LIKELY<br/>(a label is never raised)"]
    DOWN --> OUT

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class S,O,OR,R,READ,ACC,TXT,GAS,OWN,REP,CHK,OUT,DOWN done
```

Steps 1 and 3 came in after the evaluation showed the weak point of starting only from the text at the top (D51).

---

## 3. What happens on one `explain` call

Retries, the time budget and the detours, in the order they happen. Every failure becomes a **gap**, never a technical error on screen.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant C as Terminal or API
    participant K as Cache
    participant B as Bundle builder
    participant E as Explorer (Blockscout)
    participant R as Network node
    participant L as AI

    U->>C: hash + mode (+ question)
    C->>K: answered before? (network, hash, code and config version)
    alt in the cache and not expired
        K-->>C: the same facts (and the same written answer, if any)
    else
        C->>B: build(hash), 30 s budget
        par explorer and node at the same time
            B->>E: transaction, transfers, internal calls, events, ABIs
        and
            B->>R: transaction and receipt (independent copy), right chain?
        end
        Note over B: each step isolated:<br/>one broken part = one gap
        B->>R: if it failed: state reads at the parent block
        B-->>C: evidence bundle (facts + sources + gaps)
        C->>K: kept only if the transaction is final<br/>and nothing that may show up later is missing
    end
    opt written answer
        C->>L: only the facts and the gaps, then the reader's question<br/>(no API or node addresses)
        L-->>C: prose citing [E1], [E3]...
        C->>C: check: anything outside the facts?<br/>one retry, else withheld
    end
    C-->>U: answer + facts + missing data
```

---

## 4. The chat with tools

After the explanation, the reader can ask more. **The model runs nothing**: when it needs data it asks for it in JSON, and our code checks the request, runs it and returns it as a new fact with a source.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant P as Page or terminal
    participant S as Chat session
    participant L as AI
    participant T as Tools (our code)
    participant R as Node / explorer / repo

    U->>P: "how much did the recipient hold before?"
    P->>S: question (the session starts from the page's facts)
    S->>L: facts + question
    L-->>S: {"tools": [{"tool": "read", "function": "balanceOf(address)", ...}]}
    S->>T: checks the request (address, types, block, limits)
    T->>R: eth_call at the parent block<br/>+ the token's decimals() and symbol()
    R-->>T: 33540 (6 decimals, "USD₮")
    T-->>S: new fact E13: "33540, that is 0.03354 USD₮"
    S->>L: facts (with E13) + question
    L-->>S: "the recipient held 0.03354 USD₮ [E13]"
    S->>S: check (values the model chose in its request do not count as proof)
    S-->>P: answer + new fact + 👍/👎
```

Tools: `read` (a contract's state), `code` (a function's code), `file` (a file of a configured repo), `transaction` (another transaction). At most 3 per question and 10 questions per conversation.

---

## 5. How quality is measured

```mermaid
flowchart LR
    T["Automated tests<br/>~960, offline,<br/>with real recorded answers"] --> Q["Quality"]
    E["anychain eval<br/>10 real transactions,<br/>the model really writes"] --> Q
    A["Acceptance<br/>1,800 sampled transactions,<br/>checked against the node"] --> Q
    V["Clean-context reviews<br/>look for errors"] --> Q
    Q --> M["What the eval measures:<br/>status and diagnosis accuracy,<br/>citation coverage,<br/>hallucination, degradation,<br/>time and cost"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class T,E,A,V,Q,M done
```

Eval results are in `eval/report.md`; acceptance results in `docs/acceptance-report.md`.

---

## 6. Code map

Which file does what, and who calls whom. Suggested reading order: `service.py`, then `bundle.py`, then `diagnosis.py`.

```mermaid
flowchart LR
    subgraph entry["Entry points"]
        cli["cli.py<br/>explain, chat, batch, serve,<br/>log, metrics, eval, repos sync"]
        api["api.py + web/index.html<br/>local API and page"]
    end

    subgraph service_g["One answer"]
        service["service.py<br/>the path shared by terminal and API"]
        cache["cache.py<br/>answers kept by network and hash"]
        chat["chat.py<br/>session, tools, loop"]
        evaluate["evaluate.py<br/>the eval"]
    end

    subgraph core["Core (no AI)"]
        bundle["bundle.py<br/>builds the facts"]
        diagnosis["diagnosis.py<br/>rules, labels, steps"]
        reads["reads.py<br/>reads on the node"]
        units["units.py<br/>amounts in the token's units"]
        decoder["decoder.py"]
        solidity["solidity.py<br/>source index"]
        security["security.py<br/>security notes"]
        chains["chains.py<br/>network profiles"]
        models["models.py<br/>Fact, Gap, Bundle"]
    end

    subgraph collectors["collectors/"]
        explorer["explorer.py"]
        rpc["rpc.py"]
        repo["repo.py"]
        signatures["signatures.py"]
        replay["replay.py<br/>recorded real traffic, for tests"]
    end

    subgraph output["Output"]
        answer["answer.py<br/>structured answer"]
        writer["writer.py<br/>AI backends"]
        validator["validator.py<br/>the check"]
        render["render.py<br/>markdown without AI"]
        events["events.py + metrics.py<br/>log and metrics"]
    end

    cli --> service
    api --> service
    cli --> chat
    api --> chat
    cli --> evaluate
    service --> cache
    service --> bundle
    service --> writer
    service --> events
    chat --> reads
    chat --> writer
    chat --> validator
    bundle --> diagnosis
    bundle --> explorer
    bundle --> rpc
    bundle --> decoder
    bundle --> solidity
    bundle --> security
    bundle --> repo
    bundle --> signatures
    bundle --> chains
    bundle --> models
    diagnosis --> reads
    reads --> units
    writer --> validator
    api --> answer
    cli --> render
    evaluate --> replay
```

---

## 7. How a gap is classified

The "worth retrying" flag is what would let production send only network failures to a queue (D11).

```mermaid
flowchart TD
    F["A source failed"] --> Q1{"What kind of failure?"}
    Q1 -->|"timeout, dropped connection,<br/>429, 5xx, HTML page"| N["Network problem"]
    Q1 -->|"404, other 4xx,<br/>invalid JSON-RPC params"| RQ["Request problem"]
    Q1 -->|"node says busy<br/>(JSON-RPC -32005)"| N
    N --> RT["Retry now: up to 3 times<br/>within the time budget"]
    RT -->|still failing| G1["Gap, worth retrying = yes<br/>'try again in a few minutes'"]
    RT -->|back| OK["Fact recorded"]
    RQ --> G2["Gap, worth retrying = no<br/>says what would be needed"]

    classDef gap fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef ok fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class G1,G2 gap
    class OK ok
```

Besides "worth retrying", every gap carries a **cause**, which is what the event log counts (D24):

| Cause | Meaning | A problem? |
|---|---|---|
| `source_unavailable` | the source did not answer (timeout, 5xx, rate limit) | yes, alert if the rate rises |
| `source_error` | the source refused or sent something unreadable (4xx, node error, missing field) | yes |
| `source_behind` | the explorer has not indexed it yet, or disagrees with the node | yes, if it persists |
| `processing_error` | our code broke on that data, or our arithmetic contradicts a source | yes, always a defect |
| `config_error` | the configuration does not match the sources (wrong chain id or network type) | yes |
| `pending` | the transaction is not mined yet | no |
| `not_interpretable` | no ABI, truncated list, unknown field, hash not found: an expected limit | no |

---

## 8. Event log, metrics and alerts

Every answer becomes one row in a local SQLite file: network, hash, duration, gaps with cause, diagnosis label and rule, ABI source, cache state, mode, tokens, cost and the 👍/👎. `anychain log` lists problems; `anychain metrics` prints usage numbers, each with the SQL that computes it. In production the same event would go to a queue and a worker would aggregate and alert: that part is design only.

```mermaid
flowchart LR
    EX["explain, chat, API, batch"] --> EV["One row per answer"]
    CAN["Acceptance (canary)"] --> EV
    EV --> DBL["data/runs.db"]
    DBL --> Q["anychain log<br/>problems by network and cause"]
    DBL --> MT["anychain metrics<br/>failure causes, labels,<br/>ABI source, satisfaction by mode,<br/>cache (answers to people only)"]
    EV -.->|"production"| QUEUE["Queue"]
    QUEUE -.-> W["Worker"]
    W -.-> AL["Alert"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64
    class EX,EV,CAN,DBL,Q,MT done
    class QUEUE,W,AL later
```

---

## Changelog of this document

- **2026-10-08, end of phase 3 (D44 to D53):** a one-minute overview; cache by network and hash and batch; metrics with SQL; local API and page; chat with tools run by our code; amounts in the token's units, read from the token itself; evaluation with 10 real cases; diagnosis that starts from the failure's origin and says every signal its conclusion leaves unexplained; the reader's question; a mined transaction without a receipt is "unknown", never "pending". New sections: how the diagnosis decides, the chat with tools, how quality is measured.
- **2026-10-08, end of phase 2.5 (D38 to D43):** the model gets the called function's code (and the reason's line when the code pins it down); heuristic security notes; ABIs from repo artifacts, the address checked against the file's own list; access-control and deadline-parameter rules; CONFIRMED / LIKELY / UNKNOWN labels on each conclusion and in the log; steps for a merchant and for a developer; structured answer (`--json`). Header and code map brought up to date (phase 2 modules were missing).
- **2026-10-08, repos and signatures (D33 to D35, PHASE2 T6 and T7):** configured repos synced to a cache, cited and compared with the verified source, used to decode unverified contracts; public signature database as candidates, event signatures proven by hash.
- **2026-10-08, diagnosis and replay (D29 to D32, PHASE2 T2 to T5):** confidence per fact, state reads, failure diagnosis, replay when the explorer has no reason.
- **2026-10-08, validator (D28, PHASE2 T1):** the written answer is checked against the evidence, retried once with the problems, else withheld.
- **2026-10-08, LLM backends (D27):** the writer runs on the local Claude Code CLI by default; the Anthropic and OpenAI APIs are config options for a deployed service.
- **2026-10-08, level C fixes (D26):** explorer requests in parallel and fetched once; the internal-call cutoff keeps every call that moved value; reverts worded as the explorer reports them.
- **2026-10-07, zkSync L1 status (D25):** the explorer's status is never asserted; the profile confirms it with the node (`node_facts`); every gap must name its cause, with the new cause `config_error`.
- **2026-10-07, event log (D24):** every gap has a cause; every answer becomes an event in local SQLite, queried with `anychain log`; production design with a queue and a worker (not built).
- **2026-10-07, typed objects (D21):** explorer and RPC answers become typed objects once, in `collectors/types.py`.
- **2026-10-07, chain-type profiles (D20):** config names the Blockscout CHAIN_TYPE; profiles for six network types; address labels in config.
- **2026-10-07, phase 1 review rounds:** EIP-7702 delegates as an ABI source; accounts without code today are never asserted codeless (D18).
