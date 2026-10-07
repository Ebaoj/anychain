# AnyChain: instruções para o Claude

Case técnico (AI GTM Engineer, CloudWalk). A spec completa foi colada pelo Joabe no início do
projeto; decisões em `docs/DECISIONS.md`, pesquisa das redes em `docs/CHAINS.md`, fases futuras em
`docs/ROADMAP.md`, arquitetura em `docs/ARCHITECTURE.md` (+ `.pt-BR.md`, que é a que o Joabe lê;
atualizar as duas ao fim de cada fase).

## Regras que nasceram de erros reais neste projeto (07/10/2026)

1. **Nada sobre um sistema externo sem fonte verificada nesta sessão.** Nome de campo, endereço,
   casas decimais, comportamento de contrato, formato de payload: só entra no código, num texto
   ou numa resposta ao Joabe depois de visto no código-fonte, na doc oficial, numa chamada real à
   rede ou numa resposta gravada. Escrever a fonte ao lado (comentário no YAML, linha no
   DECISIONS). *Erros que geraram a regra:* o campo `op_interop` foi inventado (o real é
   `op_interop_messages`); "contrato nativo não tem ABI" foi afirmado sem conferir (a ponte da
   Rootstock tem ABI publicada).
2. **Na dúvida, a ferramenta declara; não afirma.** Se um fato não pode ser provado com os dados
   disponíveis, vira lacuna ("não interpretado", "não dá para confirmar"), nunca uma frase
   afirmativa. Vale para o texto do produto e para o que eu digo ao Joabe.
3. **Teste novo tem de falhar no código antigo.** Antes de dizer que um teste cobre um erro,
   rodar o teste contra a versão sem a correção (`git stash` só de `src/`, ou um worktree) e ver
   falhar. Desconfiar de teste que passa por limite, truncamento ou dado ausente (ex.: o limite
   de 30 chamadas internas escondia a duplicação na zkSync).
4. **Nunca ler e escrever o mesmo arquivo na mesma expressão** (`open(p,'w').write(open(p).read())`
   apaga o arquivo antes de lê-lo). Editar com Edit/Write, ou ler para uma variável primeiro.
5. **Commit só com a suíte verde.** O hook `scripts/hooks/guard_commit.sh` bloqueia `git commit`
   se `uv run pytest -q` falhar; `scripts/hooks/tests_after_edit.sh` roda a suíte depois de cada
   edição em `src/`, `tests/` ou `configs/` e devolve as falhas. Não contornar.
6. **Respostas congeladas mudam só de propósito.** `ANYCHAIN_UPDATE_GOLDEN=1` apenas quando a
   mudança de resposta é intencional, e ler o `git diff tests/golden` linha a linha antes do commit.
7. **Contar e citar números certos.** Quantidades (testes, fixtures, achados) vêm de um comando
   rodado na hora, não de memória.

## Critério de aceitação (aprovado pelo Joabe em 07/10/2026; detalhes em docs/ACCEPTANCE.md)

- **Nível A** (status, quem enviou/recebeu, valores movidos, taxa total, chamada decodificada,
  motivo da falha) e **nível B** (estado na L1, paymaster, fluxo de taxa, delegações,
  classificações): **zero fatos falsos**. Achado A/B é corrigido com teste que falha no código antigo.
- **Nível C** (rótulo, redação, agrupamento, detalhe que daria para dizer melhor): **não decido
  sozinho**. Cada achado vai para o Joabe, que escolhe corrigir agora ou mandar para a pendência.
- Uma fase fecha quando a amostra de **300 transações por rede**, sorteadas e checadas contra o nó
  (fonte independente), não tem fato falso de nível A ou B. Uma rodada de revisão por mudança.

## Ambiente

- `uv run pytest -q` roda tudo offline (respostas reais gravadas em `tests/fixtures/`).
- `uv run anychain explain <hash> --config configs/<rede>.yaml --no-llm` roda ao vivo.
- Gravar uma transação real: `uv run python scripts/record_fixture.py configs/<rede>.yaml <hash> <nome>`.
