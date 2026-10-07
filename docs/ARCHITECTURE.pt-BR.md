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
    BUN --> WRI["5. Redator com IA<br/>instrução por modo, cita [E#]"]
    BUN --> REN["Saída sem IA<br/>funciona sem o modelo"]
    WRI --> VAL["6. Validador<br/>números citados existem, nenhum endereço,<br/>valor ou hash inventado"]
    VAL --> OUT
    REN --> OUT

    OUT["7. Saída<br/>markdown + JSON"] --> CLI["Linha de comando<br/>anychain explain"]
    OUT --> API["API + tela web<br/>chat com ferramentas"]
    OUT --> DB["Banco SQLite<br/>métricas + avaliação"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef next fill:#fff3cd,stroke:#b8860b,color:#5d4037
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64

    class IN,CFG,EXP,RPC,DEC,PRO,BUN,WRI,REN,OUT,CLI done
    class REPO,CAS,DIAG,VAL next
    class API,DB later
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
        B->>E: transferências de token, chamadas internas, eventos
        B->>E: ABIs dos contratos (proxy -> implementação,<br/>ou contrato delegado via EIP-7702)
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
        cli["cli.py<br/>anychain explain"]
    end

    subgraph core["Núcleo"]
        config["config.py<br/>carrega e valida o YAML"]
        chains["chains.py<br/>um perfil por tipo de rede"]
        bundle["bundle.py<br/>monta as evidências"]
        decoder["decoder.py<br/>decodificação pela ABI"]
        models["models.py<br/>Evidência, Lacuna, Pacote"]
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
        writer["writer.py<br/>chamada à IA"]
        prompts["prompts/*.md<br/>uma instrução por modo"]
    end

    cli --> config
    cli --> bundle
    cli --> render
    cli --> writer
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

---

## Histórico deste documento

- **07/10/2026, fim da Fase 1:** primeira versão. Linha de montagem, sequência de um `explain`, mapa do código, classificação das lacunas.
- **07/10/2026, rodadas de revisão da Fase 1:** contratos delegados via EIP-7702 como fonte de ABI; conta sem código hoje nunca é dada como "sem código" no momento da transação (decisão D18); a IA não recebe endereços de API nem do nó.
- **07/10/2026:** criada esta versão em português.
- **07/10/2026, objetos tipados (D21):** as respostas do explorador e do nó viram objetos tipados uma única vez, em `collectors/types.py`.
- **07/10/2026, perfis por tipo de rede (D20):** a configuração declara o CHAIN_TYPE do Blockscout; perfis para seis tipos de rede; rótulos de endereços na configuração.
