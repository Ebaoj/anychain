# Arquitetura

Documento vivo: atualizado ao fim de cada fase. Versão em português de `ARCHITECTURE.md` (a versão em inglês é a da entrega).

As cores mostram o que já existe:

- **Verde**: construído e testado
- **Amarelo**: em construção nesta fase
- **Cinza**: fases seguintes
- Caixas brancas são só agrupamentos.

Estado atual: **fim da Fase 4** (08/10/2026): triagem (uma pergunta antes de responder, escolhida pelo código), linha do tempo do remetente em volta de uma falha, notas de gás, Dockerfile, README final e a rede privada de demonstração (Anvil + Blockscout próprio + BRLC atrás de um proxy, lida só com `configs/devnet.yaml`).

---

## 0. O que você está construindo, em um minuto

Um assistente que **explica uma transação de blockchain** para quem não é técnico (um lojista: "por que meu pagamento não passou?") e para quem é (um desenvolvedor, um auditor), em **qualquer rede EVM**, trocando só a configuração.

A ideia que guia tudo: **a IA não descobre nada, só escreve.** Quem descobre é o código, que dá para testar.

```mermaid
flowchart LR
    Q["1. Pergunta<br/>o hash da transação<br/>e para quem é a resposta"] --> F
    F["2. Fatos, sem IA<br/>explorador + nó da rede<br/>cada fato numerado (E1, E2...)<br/>com a fonte de onde veio"] --> D
    D["3. Conclusão, sem IA<br/>regras de diagnóstico<br/>leituras no nó para provar<br/>CONFIRMED / LIKELY / UNKNOWN"] --> W
    W["4. Texto, com IA<br/>escreve só a partir dos fatos<br/>cita [E3] em cada frase"] --> C
    C["5. Checagem, sem IA<br/>todo número, endereço e link<br/>precisa estar nos fatos<br/>senão: tenta 1 vez, depois retém"] --> R["6. Resposta<br/>terminal, API, página web<br/>e chat para perguntar mais"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class Q,F,D,W,C,R done
```

Por que assim:

- **Se a IA errar, a checagem pega.** Um número que não está nos fatos não chega ao cliente.
- **Se uma regra errar, dá para achar e corrigir com um teste.** Foi o que aconteceu na Fase 3: a avaliação achou uma regra que ignorava uma chamada interna sem gás; ela foi corrigida, e o caso real virou teste (decisões D50 e D51).
- **Quando falta dado, a resposta diz o que falta** ("o explorador não respondeu; tente de novo"), em vez de adivinhar.

---

## 1. A linha de montagem

Os mesmos passos, com as peças de cada um.

```mermaid
flowchart TD
    IN["Entrada<br/>hash + modo (lojista, desenvolvedor, auditor)<br/>+ pergunta opcional"] --> TRI{"Triagem<br/>não é hash? 'não recebi'?<br/>palavras emprestadas?<br/>(no máximo 1 pergunta)"}
    TRI --> CACHE
    CACHE{"Já respondida antes?<br/>cache por rede e hash<br/>(só transações finalizadas e completas)"} -->|sim| BUN
    CACHE -->|não| COL
    CFG["Configuração (YAML)<br/>explorador, nó, repositórios,<br/>modelo de IA, tipo de rede<br/>um arquivo por rede"] --> COL

    subgraph COL["1. Coletores"]
        EXP["Explorador<br/>API Blockscout v2"]
        RPC["Nó da rede<br/>JSON-RPC (confere o explorador)"]
        REPO["Repositórios<br/>código e ABIs publicadas"]
    end

    COL --> DEC["2. Decodificador<br/>chamada, eventos, chamadas internas<br/>cascata de ABI: explorador, artefatos do repo,<br/>código do repo, base de assinaturas, dado cru"]
    DEC --> PRO["Perfil do tipo de rede<br/>taxas, estado na L1, depósitos"]
    DEC --> CODE["Código da função chamada<br/>e notas de segurança (heurísticas)"]
    DEC --> DIAG["3. Diagnóstico (seção 2)<br/>só se falhou"]
    DIAG --> TL["Linha do tempo do remetente<br/>e notas de gás<br/>(no fim, sem renumerar fatos)"]
    TL --> BUN
    PRO --> BUN
    CODE --> BUN
    DIAG --> BUN

    BUN["4. Pacote de evidências<br/>fatos E1, E2... com fonte e confiança<br/>lacunas: o que falta e por quê"]
    BUN --> WRI["5. Redator com IA<br/>uma instrução por modo<br/>Claude Code (padrão), APIs Anthropic ou OpenAI"]
    WRI --> VAL["6. Checagem<br/>citações, números, endereços, links,<br/>valores na unidade do token"]
    VAL --> OUT["7. Saídas"]
    BUN --> OUT

    OUT --> CLI["Terminal<br/>explain, chat, batch"]
    OUT --> API["API local<br/>/explain /chat /health /feedback"]
    API --> PAGE["Página web<br/>com chat e 👍/👎"]
    OUT --> LOG["Log de eventos<br/>uma linha por resposta"]
    LOG --> MET["anychain log e anychain metrics<br/>(cada número com o seu SQL)"]
    EVAL["anychain eval<br/>11 casos reais, medidos"] -.usa.-> BUN

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class IN,TRI,TL,CACHE,CFG,EXP,RPC,REPO,DEC,PRO,CODE,DIAG,BUN,WRI,VAL,OUT,CLI,API,PAGE,LOG,MET,EVAL done
    style COL fill:#ffffff,stroke:#90a4ae,color:#263238
```

| Fase | O que entra |
|---|---|
| 1 (pronta) | configuração, coletores do explorador e do nó, decodificador, pacote de evidências, redator com IA, terminal |
| 2 (pronta) | diagnóstico de falhas, leituras de estado, repositórios, cascata de ABI, confiança de cada fato, checagem da resposta |
| 2.5 (pronta) | código da função, notas de segurança, ABIs de artefatos, controle de acesso, prazo pelo parâmetro, rótulos, passos por leitor, resposta estruturada |
| 3 (pronta) | cache e lote, métricas, API, chat com ferramentas, página, conversão para a unidade do token, avaliação, diagnóstico pela origem da falha, pergunta do usuário, página de impacto, demonstração gravada |
| 4 | **pronto:** triagem (endereço em vez de hash, "não recebi", palavras emprestadas de outro contrato), linha do tempo do remetente com padrões (tentou de novo e deu certo, aprovou e deu certo, falhas seguidas), notas de gás, Dockerfile, README final com conversas reais. rede privada de demonstração (`devnet/`). **Fica para depois:** trace do nó |

---

## 2. Como o diagnóstico decide (só quando a transação falhou)

Regras escritas em código, na ordem abaixo. Cada conclusão sai com um rótulo: **CONFIRMED** (provada pelo motivo decodificado ou por uma leitura no nó), **LIKELY** (sugerida por um padrão) ou **UNKNOWN** (não há dado para concluir, e a resposta diz o que falta).

```mermaid
flowchart TD
    S["A transação falhou"] --> O{"1. Onde começou?<br/>alguma chamada interna falhou com erro<br/>de execução (ex.: 'out of gas')?"}
    O -->|sim| OR["A origem é essa chamada<br/>o erro lá em cima é consequência<br/>(ex.: TRANSFER_FROM_FAILED por falta de gás dentro)"]
    O -->|não| R{"2. Qual o motivo lá em cima?"}
    R -->|"saldo, allowance,<br/>roteador sem conseguir puxar o token"| READ["Lê no nó, no bloco anterior:<br/>saldo, aprovação, casas decimais<br/>menor que o pedido? CONFIRMED"]
    R -->|"Ownable, AccessControl"| ACC["Lê owner() ou hasRole()<br/>confirma só se o erro nomeia o remetente"]
    R -->|"pausado, slippage,<br/>prazo"| TXT["Significado do texto<br/>(prazo: compara o parâmetro<br/>com o horário do bloco)"]
    R -->|"out of gas"| GAS["Todo o gás usado?<br/>o recibo do nó confere? CONFIRMED"]
    R -->|"outro texto do contrato"| OWN["O motivo do próprio contrato<br/>o significado está no código dele"]
    R -->|"nenhum motivo"| REP["Repete a chamada no nó<br/>(o resultado é só uma pista: LIKELY)"]
    OR --> CHK
    READ --> CHK
    ACC --> CHK
    TXT --> CHK
    GAS --> CHK
    OWN --> CHK
    REP --> CHK
    CHK{"3. A conclusão explica todos os sinais?<br/>erro interno não citado,<br/>todo o gás usado sem ser sobre gás"}
    CHK -->|sim| OUT["Conclusão com o rótulo<br/>+ passos para o lojista e para o desenvolvedor"]
    CHK -->|não| DOWN["Diz o sinal que sobrou<br/>e CONFIRMED vira LIKELY<br/>(nunca sobe um rótulo)"]
    DOWN --> OUT

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    class S,O,OR,R,READ,ACC,TXT,GAS,OWN,REP,CHK,OUT,DOWN done
```

Os passos 1 e 3 entraram depois que a avaliação mostrou o ponto fraco de partir só do texto do topo (decisão D51).

---

## 3. O que acontece num `explain`

As novas tentativas, o teto de tempo e os desvios, na ordem em que acontecem. Toda falha vira uma **lacuna**, nunca um erro técnico na tela.

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant C as Terminal ou API
    participant K as Cache
    participant B as Montador do pacote
    participant E as Explorador (Blockscout)
    participant R as Nó da rede
    participant L as IA

    U->>C: hash + modo
    C->>K: já respondida? (rede, hash, versão do código e da configuração)
    alt está no cache e não expirou
        K-->>C: os mesmos fatos (e a mesma resposta escrita, se houver)
    else
        C->>B: montar(hash), teto de 30 s
        par explorador e nó ao mesmo tempo
            B->>E: transação, transferências, chamadas internas, eventos, ABIs
        and
            B->>R: transação e recibo (cópia independente), rede certa?
        end
        Note over B: cada etapa isolada:<br/>uma parte quebrada = uma lacuna
        B->>R: se falhou: leituras de estado no bloco anterior
        B-->>C: pacote de evidências (fatos + fontes + lacunas)
        C->>K: guarda só se a transação está finalizada<br/>e não falta nada que possa aparecer depois
    end
    opt resposta escrita
        C->>L: só os fatos e as lacunas<br/>(sem endereços de API nem do nó)
        L-->>C: texto citando [E1], [E3]...
        C->>C: checagem: algo fora dos fatos?<br/>tenta de novo 1 vez, senão retém
    end
    C-->>U: resposta + fatos + dados faltantes
```

---

## 4. O chat com ferramentas

Depois da explicação, a pessoa pode perguntar mais. **O modelo não executa nada**: quando precisa de um dado, pede em JSON, e o nosso código confere o pedido, executa e devolve como fato novo, com fonte.

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant P as Página ou terminal
    participant S as Sessão do chat
    participant L as IA
    participant T as Ferramentas (nosso código)
    participant R as Nó / explorador / repositório

    U->>P: "quanto o destinatário tinha antes?"
    P->>S: pergunta (a sessão parte dos mesmos fatos da página)
    S->>L: fatos + pergunta
    L-->>S: {"tools": [{"tool": "read", "function": "balanceOf(address)", ...}]}
    S->>T: confere o pedido (endereço, tipos, bloco, limites)
    T->>R: eth_call no bloco anterior<br/>+ decimals() e symbol() do token
    R-->>T: 33540 (6 casas, "USD₮")
    T-->>S: fato novo E13: "33540, ou seja 0,03354 USD₮"
    S->>L: fatos (com o E13) + pergunta
    L-->>S: "o destinatário tinha 0,03354 USD₮ [E13]"
    S->>S: checagem (os valores que o modelo escolheu no pedido não contam como prova)
    S-->>P: resposta + fato novo + 👍/👎
```

Ferramentas: `read` (estado de um contrato), `code` (código de uma função), `file` (arquivo de um repositório configurado), `transaction` (outra transação). No máximo 3 por pergunta e 10 perguntas por conversa.

---

## 5. Como a qualidade é medida

```mermaid
flowchart LR
    T["Testes automáticos<br/>~1.050, offline,<br/>com respostas reais gravadas"] --> Q["Qualidade"]
    E["anychain eval<br/>11 transações reais,<br/>o modelo escreve de verdade"] --> Q
    A["Aceitação<br/>1.800 transações sorteadas,<br/>conferidas contra o nó"] --> Q
    V["Revisão de contexto limpo<br/>a cada etapa, procura erros"] --> Q
    Q --> M["Números que o eval mede:<br/>acerto de status e diagnóstico,<br/>cobertura de citações,<br/>alucinação, degradação,<br/>tempo e custo"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef next fill:#fff3cd,stroke:#b8860b,color:#5d4037
    class T,E,A,V,Q,M done
```

Os resultados do eval ficam em `eval/report.md`; os da aceitação, em `docs/acceptance-report.md`.

---

## 6. Mapa do código

Qual arquivo faz o quê, e quem chama quem. Ordem sugerida de leitura: `service.py`, depois `bundle.py`, depois `diagnosis.py`.

```mermaid
flowchart LR
    subgraph entry["Entradas"]
        cli["cli.py<br/>explain, chat, batch, serve,<br/>log, metrics, eval, repos sync, llm"]
        api["api.py + web/index.html<br/>API local e a página de conversa"]
        networks["networks.py<br/>troca ou adiciona rede<br/>sem reiniciar"]
        settings["llm_settings.py<br/>o modelo, a chave, o idioma<br/>(salvos fora do repo, 0600)"]
    end

    subgraph service_g["Uma resposta"]
        service["service.py<br/>o caminho comum do terminal e da API"]
        cache["cache.py<br/>respostas guardadas por rede e hash"]
        chat["chat.py<br/>sessão, ferramentas, laço"]
        evaluate["evaluate.py<br/>o eval"]
    end

    subgraph core["Núcleo (sem IA)"]
        bundle["bundle.py<br/>monta os fatos"]
        diagnosis["diagnosis.py<br/>regras, rótulos, passos"]
        reads["reads.py<br/>leituras no nó"]
        units["units.py<br/>valor na unidade do token"]
        decoder["decoder.py"]
        solidity["solidity.py<br/>índice do código"]
        security["security.py<br/>notas de segurança"]
        chains["chains.py<br/>perfis de rede"]
        models["models.py<br/>Fato, Lacuna, Pacote"]
    end

    subgraph collectors["collectors/"]
        explorer["explorer.py"]
        rpc["rpc.py"]
        repo["repo.py"]
        signatures["signatures.py"]
        replay["replay.py<br/>tráfego real gravado, para testes"]
    end

    subgraph output["Saída"]
        answer["answer.py<br/>resposta estruturada"]
        outline["outline.py<br/>as seções da resposta,<br/>montadas a partir dos fatos"]
        writer["writer.py<br/>motores de IA"]
        validator["validator.py<br/>checagem"]
        render["render.py<br/>markdown sem IA"]
        events["events.py + metrics.py<br/>log e métricas"]
    end

    cli --> service
    api --> service
    api --> networks
    api --> settings
    cli --> settings
    writer --> outline
    writer --> settings
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

## 7. Como uma lacuna é classificada

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

## 8. Log de eventos, métricas e alertas

Cada resposta vira uma linha num arquivo SQLite local: rede, hash, duração, lacunas com causa, rótulo e regra do diagnóstico, origem da ABI, estado do cache, modo, tokens, custo e o 👍/👎. `anychain log` mostra problemas; `anychain metrics` mostra os números do uso, cada um com o SQL que o calcula. Em produção o mesmo evento iria para uma fila, e um worker agregaria e alertaria: esse trecho é só desenho.

```mermaid
flowchart LR
    EX["explain, chat, API, lote"] --> EV["Uma linha por resposta"]
    CAN["Aceitação (canário)"] --> EV
    EV --> DBL["data/runs.db"]
    DBL --> Q["anychain log<br/>problemas por rede e causa"]
    DBL --> MT["anychain metrics<br/>causas de falha, rótulos,<br/>origem da ABI, satisfação por modo,<br/>cache (só respostas a pessoas)"]
    EV -.->|"produção"| QUEUE["Fila"]
    QUEUE -.-> W["Worker"]
    W -.-> AL["Alerta"]

    classDef done fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef later fill:#eceff1,stroke:#90a4ae,color:#455a64
    class EX,EV,CAN,DBL,Q,MT done
    class QUEUE,W,AL later
```

---

## Histórico deste documento

- **09/10/2026, depois da Fase 4 (D59 a D67):** rede privada montada como a da CloudWalk (nó local, Blockscout próprio, BRLC atrás de um proxy), lida só com um arquivo de configuração; o modelo e a chave de API escolhidos pelo terminal ou pela página, com o modelo vindo da lista do próprio provedor; modelos menores confiáveis por código (roteiro da resposta montado a partir dos fatos, só os próximos passos do leitor, citações normalizadas, fatos-chave obrigatórios); rede trocada ou adicionada pela página sem reiniciar; a página virou uma conversa em três versões (lojista, desenvolvedor, auditor); linguagem simples para o lojista. Novos no mapa do código: `outline.py`, `networks.py`, `llm_settings.py`.
- **08/10/2026, Fase 4 (D55 a D58):** triagem com uma pergunta escolhida pelo código; linha do tempo do remetente em volta de uma falha, com padrões ditos só sobre nonces contínuos; notas de gás (heurísticas); Dockerfile, testado num servidor; README final.
- **08/10/2026, Fase 3 até a T5 (D44 a D51):** seção 0 em linguagem simples; cache por rede e hash e lote; métricas com SQL; API local e página; chat com ferramentas executadas pelo nosso código; valores na unidade do token, lidos do próprio token; avaliação com 10 casos reais; diagnóstico que parte da origem da falha e diz todo sinal que a conclusão não explica. Novas seções: como o diagnóstico decide, o chat com ferramentas, como a qualidade é medida.
- **08/10/2026, fim da Fase 2.5 (D38 a D43):** código da função como fato, notas de segurança, ABI de artefatos, controle de acesso, prazo pelo parâmetro, rótulos, passos por leitor, resposta estruturada.
- **07/10/2026, fim da Fase 1:** primeira versão. Linha de montagem, sequência de um `explain`, mapa do código, classificação das lacunas.
- **07/10/2026, rodadas de revisão da Fase 1:** contratos delegados via EIP-7702 como fonte de ABI; conta sem código hoje nunca é dada como "sem código" no momento da transação (D18); a IA não recebe endereços de API nem do nó.
- **07/10/2026:** criada esta versão em português.
- **07/10/2026, objetos tipados (D21):** as respostas do explorador e do nó viram objetos tipados uma única vez, em `collectors/types.py`.
- **08/10/2026, Fase 2 (D24 a D37):** log de eventos, motores de IA, checagem da resposta, confiança de cada fato, leituras de estado, diagnóstico, repetição, repositórios, base de assinaturas, matriz de degradação.
- **07/10/2026, perfis por tipo de rede (D20):** a configuração declara o tipo de rede do Blockscout; perfis para seis tipos.
