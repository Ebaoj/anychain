# Arquitetura

Documento vivo: atualizado ao fim de cada fase. Versão em português de `ARCHITECTURE.md` (a versão em inglês é a da entrega).

As cores mostram o que já existe:

- **Verde**: construído e testado
- **Amarelo**: próxima fase
- **Cinza**: fases seguintes
- Caixas brancas são só agrupamentos.

Estado atual: **fim da Fase 1** (07/10/2026).

---

## 1. A linha de montagem

Uma regra molda tudo: os fatos são coletados **sem nenhuma IA**, numerados e acompanhados da fonte. A IA só escreve o texto a partir desses fatos e, a partir da Fase 2, um validador confere se ela não inventou nada.

```mermaid
flowchart TD
    IN["Entrada<br/>hash da transação + modo + pergunta"] --> CFG

    CFG["Configuração (YAML)<br/>explorador, nó RPC, repositórios, modelo de IA,<br/>tipo de rede, rótulos de endereços<br/>um arquivo por rede"]

    CFG --> COL
    subgraph COL["1. Coletores"]
        EXP["Cliente do explorador<br/>API Blockscout v2"]
        RPC["Cliente do nó<br/>JSON-RPC"]
        REPO["Cliente de repositório<br/>clone do GitHub + índice"]
    end

    COL --> DEC["2. Decodificador<br/>chamadas, eventos, eventos anônimos<br/>ABI do explorador, delegações EIP-7702"]
    DEC --> PRO["Perfil do tipo de rede<br/>default, ethereum, optimism,<br/>optimism-celo, rsk, zksync<br/>taxas, estado na L1, depósitos"]
    PRO --> BUN
    DEC --> CAS["Cascata de ABI<br/>artefatos do repo, assinaturas do código,<br/>4byte, dado cru"]
    DEC --> DIAG["3. Diagnóstico<br/>só se falhou: motivo do revert,<br/>leituras eth_call, regras"]
    DEC --> BUN
    CAS --> BUN
    DIAG --> BUN

    BUN["4. Pacote de evidências<br/>fatos E1, E2... cada um com fonte<br/>lacunas: o que falta + vale tentar de novo?"]
    BUN --> WRI["5. Redator com IA<br/>instrução por modo, cita [E#]<br/>Claude Code (padrão), API Anthropic ou OpenAI"]
    BUN --> REN["Saída sem IA<br/>funciona sem o modelo"]
    WRI --> VAL["6. Validador<br/>ids citados existem, nenhum endereço,<br/>valor ou hash inventado<br/>tenta de novo uma vez, senão só os fatos"]
    VAL --> OUT
    REN --> OUT

    OUT["7. Saída<br/>markdown + JSON"] --> CLI["Linha de comando<br/>anychain explain"]
    OUT --> API["API + tela web<br/>chat com ferramentas"]
    OUT --> DB["Banco SQLite<br/>métricas + avaliação"]
    BUN --> LOG["Log de eventos<br/>uma linha por resposta:<br/>lacunas com causa, travamentos,<br/>conferência contra o nó"]
    LOG --> QRY["anychain log<br/>consulta por rede e causa"]
    LOG -.produção, só desenho.-> FILA["Fila (ex.: SQS)"]
    FILA -.-> WRK["Worker<br/>agrega por rede e causa,<br/>dispara alerta"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef next fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64

    class IN,CFG,EXP,RPC,DEC,PRO,BUN,WRI,REN,OUT,CLI,LOG,QRY,VAL done
    class REPO,CAS,DIAG next
    class API,DB,FILA,WRK later
    style COL fill:#ffffff,stroke:#90a4ae,color:#263238
```

| Fase | O que entra |
|---|---|
| 1 (pronta) | configuração, coletores do explorador e do nó, decodificador com a ABI do explorador, pacote de evidências, redator com IA, linha de comando |
| 2 | diagnóstico de falhas, leituras de estado com `eth_call`, leitura dos repositórios, cascata completa de ABI, níveis de confiança, validador |
| 3 | API, tela web, chat com ferramentas, modos, SQLite + métricas, suíte de avaliação |
| 4 | perguntas de triagem, linha do tempo entre transações, notas de segurança, notas de gás |

---

## 2. O que acontece num `explain`

As novas tentativas, o teto de tempo e os desvios, na ordem em que acontecem. Toda falha vira uma **lacuna**, nunca um erro técnico na tela.

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant C as Linha de comando
    participant B as Montador do pacote
    participant E as Explorador (Blockscout)
    participant R as Nó RPC
    participant L as IA (Claude)

    U->>C: anychain explain 0x... --config eth.yaml
    C->>B: montar(hash), teto de 30 s
    B->>E: GET /transactions/{hash}
    Note over B,E: falha de rede: até 3 tentativas<br/>404 ou pedido errado: sem nova tentativa
    E-->>B: transação (ou lacuna)
    B->>R: eth_chainId (rede certa?)
    B->>R: eth_getTransactionByHash + recibo
    R-->>B: cópia independente (ou lacuna)
    alt explorador respondeu
        par em paralelo, cada pedido uma vez só (D26)
            B->>E: transferências de token, chamadas internas, eventos
        and
            B->>E: ABIs do contrato chamado e dos emissores de eventos<br/>(proxy -> implementação, ou contrato delegado via EIP-7702)
        end
        Note over B: conta sem código hoje?<br/>só diz "sem código" se esta transação desligou a delegação,<br/>senão "não dá para confirmar" + lacuna
        Note over B: cada etapa isolada:<br/>uma parte quebrada = uma lacuna
    else explorador fora do ar ou atrasado
        Note over B: resumo só com o nó,<br/>a lacuna diz o que falta
    end
    B-->>C: pacote de evidências (fatos + fontes + lacunas)
    opt IA disponível e sem --no-llm
        C->>L: só os fatos, links do explorador e lacunas<br/>(sem endereços de API nem do nó)
        L-->>C: explicação citando [E1], [E3]...
    end
    C-->>U: explicação + lista de evidências + dados faltantes
```

---

## 3. Mapa do código

Qual arquivo faz o quê, e quem chama quem. Ordem sugerida de leitura: `cli.py`, depois `bundle.py`, depois os coletores.

```mermaid
flowchart LR
    subgraph entry["Ponto de entrada"]
        cli["cli.py<br/>anychain explain<br/>anychain log"]
    end

    subgraph core["Núcleo"]
        config["config.py<br/>carrega e valida o YAML"]
        chains["chains.py<br/>um perfil por tipo de rede"]
        bundle["bundle.py<br/>monta as evidências"]
        decoder["decoder.py<br/>decodificação pela ABI"]
        models["models.py<br/>Evidência, Lacuna (com causa), Pacote"]
        events["events.py<br/>log de eventos: RunEvent,<br/>SqliteEventLog, consultas"]
    end

    subgraph collectors["collectors/"]
        http["http.py<br/>novas tentativas, teto de tempo,<br/>erro de rede ou de pedido"]
        types["types.py<br/>objetos tipados: Transaction,<br/>InternalCall, TokenTransfer, Log..."]
        explorer["explorer.py<br/>Blockscout"]
        rpc["rpc.py<br/>JSON-RPC"]
        replay["replay.py<br/>grava e repete tráfego real<br/>para os testes"]
    end

    subgraph output["Saída"]
        render["render.py<br/>markdown, sem IA"]
        writer["writer.py<br/>motores de IA: claude_code,<br/>anthropic, openai"]
        validator["validator.py<br/>confere a resposta escrita<br/>contra os fatos"]
        prompts["prompts/*.md<br/>uma instrução por modo"]
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
    writer --> validator
    replay -.usado nos testes.-> explorer
    replay -.usado nos testes.-> rpc
```

---

## 4. Como uma lacuna é classificada

A marcação "vale tentar de novo" é o que permitiria, em produção, mandar para uma fila só as falhas de rede (decisão D11).

```mermaid
flowchart TD
    F["Uma fonte falhou"] --> Q1{"Que tipo de falha?"}
    Q1 -->|"tempo esgotado, conexão caiu,<br/>429, 5xx, página HTML"| N["Problema de rede"]
    Q1 -->|"404, outro 4xx,<br/>parâmetro JSON-RPC inválido"| RQ["Problema no pedido"]
    Q1 -->|"nó diz que está ocupado<br/>(JSON-RPC -32005)"| N
    N --> RT["Tenta de novo agora: até 3 vezes<br/>dentro do teto de tempo"]
    RT -->|continua falhando| G1["Lacuna, vale tentar de novo = sim<br/>'tente em alguns minutos'"]
    RT -->|voltou| OK["Fato registrado"]
    RQ --> G2["Lacuna, vale tentar de novo = não<br/>diz o que seria preciso"]

    classDef gap fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef ok fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class G1,G2 gap
    class OK ok
```

Além de "vale tentar de novo", cada lacuna leva uma **causa**, que é o que o log de eventos conta (decisão D24):

| Causa | O que quer dizer | É problema? |
|---|---|---|
| `source_unavailable` | a fonte não respondeu (tempo esgotado, 5xx, limite de uso) | sim, alerta se a taxa sobe |
| `source_error` | a fonte recusou ou mandou algo ilegível (4xx, erro do nó, campo faltando) | sim |
| `source_behind` | o explorador ainda não indexou, ou discorda do nó | sim, se persistir |
| `processing_error` | nosso código quebrou com aquele dado, ou nossa conta contradiz uma fonte | sim, sempre é defeito |
| `config_error` | a configuração não bate com as fontes (chain id ou tipo de rede errado) | sim |
| `pending` | a transação ainda não foi minerada | não |
| `not_interpretable` | sem ABI, lista truncada, campo desconhecido, hash não encontrado: limite esperado | não |

---

## 5. Log de eventos e alertas

Hoje o log é um arquivo SQLite local, consultado com `anychain log`. Em produção o mesmo evento iria para uma fila, e um worker agregaria e alertaria. Esse trecho é só desenho, não foi construído.

```mermaid
flowchart LR
    EX["anychain explain<br/>(e depois a API)"] --> EV["RunEvent<br/>rede, hash, duração,<br/>lacunas com causa, travamento"]
    CAN["Canário: script de aceitação<br/>(hoje rodado à mão; o agendamento<br/>diário é desenho de produção)"] --> EV
    EV --> SINK{"storage.event_sink"}
    SINK -->|sqlite| DBL["data/runs.db"]
    DBL --> Q["anychain log<br/>totais por rede e causa,<br/>--problems lista os casos"]
    SINK -.->|"fila (produção)"| QUEUE["Fila"]
    QUEUE -.-> W["Worker"]
    W -.-> AL["Alerta<br/>travamento ou erro de processamento: sempre<br/>fonte fora do ar acima de 5% na hora<br/>conferência do canário falhou"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64
    class EX,EV,CAN,SINK,DBL,Q done
    class QUEUE,W,AL later
```

O canário é o único jeito de pegar o erro silencioso (um fato falso que nada denunciou): por isso a rodada de aceitação grava no mesmo log, com origem `canary`.

---

## Histórico deste documento

- **07/10/2026, fim da Fase 1:** primeira versão. Linha de montagem, sequência de um `explain`, mapa do código, classificação das lacunas.
- **07/10/2026, rodadas de revisão da Fase 1:** contratos delegados via EIP-7702 como fonte de ABI; conta sem código hoje nunca é dada como "sem código" no momento da transação (decisão D18); a IA não recebe endereços de API nem do nó.
- **07/10/2026:** criada esta versão em português.
- **07/10/2026, objetos tipados (D21):** as respostas do explorador e do nó viram objetos tipados uma única vez, em `collectors/types.py`.
- **08/10/2026, validador (D28, Fase 2 T1):** a resposta escrita é conferida contra os fatos; com problema, o modelo tenta de novo uma vez; se falhar de novo, aparecem só os fatos.
- **08/10/2026, motores de IA (D27):** o redator usa o Claude Code local por padrão; as APIs da Anthropic e da OpenAI são opções de configuração para um serviço publicado.
- **08/10/2026, correções de nível C (D26):** pedidos ao explorador em paralelo e uma vez só; o corte das chamadas internas mantém todas as que moveram valor; reversões descritas como o explorador informa.
- **07/10/2026, estado na L1 da zkSync (D25):** o estado do explorador nunca é afirmado; o perfil confirma com o nó (`node_facts`); a causa de cada lacuna é obrigatória, com a nova causa `config_error`.
- **07/10/2026, log de eventos (D24):** cada lacuna ganhou uma causa; cada resposta vira um evento no SQLite local, consultado com `anychain log`; desenho de produção com fila e worker (não construído).
- **07/10/2026, perfis por tipo de rede (D20):** a configuração declara o CHAIN_TYPE do Blockscout; perfis para seis tipos de rede; rótulos de endereços na configuração.
