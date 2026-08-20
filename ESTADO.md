# Estado do Software DRE

**Estado em: 20/08/2026**

Este documento registra o estado real do software nesta data, distinguindo explicitamente o que está implementado, o que foi executado tecnicamente e o que foi validado gerencialmente. Não substitui uma apuração de referência.

---

## O que está implementado no código

As funcionalidades abaixo existem no código e foram revisadas nesta data:

- Leitura de plano de contas CSV com suporte ao formato de 9 colunas (`id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem`) e retrocompatibilidade com o formato antigo de 6 colunas.
- Resolução de mapeamento de categorias em três camadas: CSV (via `categoria_origem`) ← Supabase (`mapeamentos_cliente`) ← config manual.
- Filtro `exibir_dre=True` para excluir contas de auditoria do DRE sem removê-las do mapeamento.
- Suporte à flag `auditoria_only=True`: categoria mapeada (não aparece em `categorias_nao_mapeadas.csv`) mas excluída do DRE.
- Booleanos no CSV aceitos nas formas: `Sim/Não`, `true/false`, `1/0`, `sim/não`.
- Processador Zeus: leitura de PDFs Wingraph (AP + AR, multi-CNPJ), configurados por mês em YAML.
- Processador Zixbe: leitura de Excel Conta Azul (visão de competência).
- Geração do DRE por rollup nível 3 → 2 → 1, com linhas de resultado por fórmula.
- Validação DRE: checagem de quais `categoria_origem` dos lançamentos contribuem para o DRE (`exibir_dre=True`) e quais seriam silenciosamente ignorados.
- Persistência no Supabase (opcional, flag `--save-supabase`).
- Hierarquia de resolução de caminhos: canônico (`cfg/clientes/`) → legado com aviso.

**As alterações de código mais recentes** (implementação do novo formato do plano de contas, `exibir_dre`, `auditoria_only`, `categoria_origem`) foram feitas em `etl/plano_contas.py`, `etl/pipeline.py` e `etl/mapeamentos.py`. **O ETL não foi reexecutado após essas alterações** — os outputs em `saida/` foram gerados com versões anteriores do código.

---

## Último estado conhecido por cliente

### Zeus — 2026-01 (run: 01/06/2026, 20:39)

| Indicador | Valor |
|---|---|
| Total ERP (lançamentos) | R$ 90.308,12 |
| Total ETL | R$ 90.308,12 |
| Diferença | R$ 0,00 |
| Lançamentos processados | 345 |
| Inconsistências | 0 |
| Categorias não mapeadas | 40 |
| DRE — RECEITA BRUTA | R$ 666.458,20 |
| DRE — RESULTADO OPERACIONAL | R$ 614.857,93 |
| DRE — RESULTADO | R$ 611.808,03 |

Categorias não mapeadas neste run (40): Aluguel/Condomínio/IPTU, INSS, Serviços Terceirizados, Salários e Antecipações, Segurança, entre outras. Ver `saida/zeus/2026-01/categorias_nao_mapeadas.csv`.

**Observações:**
- O DRE Zeus 2026-01 foi gerado com uma versão diferente do plano de contas. O `plano_contas_zeus.csv` atual tem INSS/IRRF e FGTS como nível 4 (ord. 760/770), mas o DRE mostra essas contas como nível 3 (ord. 134/135). Os outputs em `saida/zeus/2026-01/` não são reproduzíveis com o plano atual sem revisão.
- FGTS aparece com R$ 4.354,47 no DRE. INSS/IRRF aparece zerado (estava na lista de não mapeadas).
- O plano foi carregado via fallback legado (`~/Downloads/plano_contas_zeus.csv`), não pelo canônico `cfg/clientes/zeus/plano_contas_cliente.csv` (que não existe).

### Zixbe — 2026-04 (run: 18/05/2026, 18:58)

| Indicador | Valor |
|---|---|
| Total ERP | R$ −239.282,36 |
| Total ETL | R$ −239.282,36 |
| Diferença | R$ 0,00 |
| Linhas origem | 733 |
| Lançamentos saída | 925 |
| Inconsistências | 0 |
| Categorias não mapeadas | 0 |
| DRE — RECEITA BRUTA | R$ 462.727,86 |

**Observações:**
- O `plano_contas_zixbe.csv` está no formato antigo (6 colunas, sem `categoria_origem`). Com o código atual, `ler_mapeamento_plano()` retorna `{}` para Zixbe — nenhuma categoria é resolvida via CSV.
- As 0 categorias não mapeadas no run de maio podem refletir mapeamentos ativos no Supabase naquele momento, não uma resolução via plano CSV.
- O DRE Zixbe foi gerado com versão anterior do código e do plano.
- O plano foi carregado via fallback legado (`~/Downloads/plano_contas_zixbe.csv`).

---

## NÃO CONFIRMADO

Os seguintes itens **não foram confirmados** e não devem ser tratados como estados validados:

- **DRE Zeus 2026-01 validado gerencialmente**: o output existe e a conferência matemática zerou, mas não há registro de que um responsável comparou o DRE com a apuração gerencial de referência para este mês.
- **DRE Zixbe 2026-04 validado gerencialmente**: mesma situação.
- **Reproduzibilidade do DRE Zeus 2026-01**: os arquivos de entrada (PDFs OneDrive) e o plano de contas utilizado no run de junho não coincidem com o estado atual do repositório.
- **Código atual sem regressões**: as alterações mais recentes (plano_contas.py, pipeline.py, mapeamentos.py) não foram testadas com uma execução real após as mudanças.
- **Mapeamento Zixbe via CSV funcional**: com o plano antigo (6 colunas), `ler_mapeamento_plano()` retorna `{}`. O mapeamento depende inteiramente do Supabase ou de config manual, e o estado atual do `mapeamentos_cliente` no Supabase não está documentado.
- **Contas de nível 4 Zeus tratadas corretamente**: INSS/IRRF (id=76, nível=4) e FGTS (id=77, nível=4) estão no plano atual mas o DRE processa apenas níveis 1–3. O comportamento gerencial correto desses itens não foi definido.

---

## Ponto de retomada

O próximo passo é tornar o processamento Zeus reproduzível no ambiente atual e validar o DRE contra uma apuração gerencial de referência. A sequência recomendada:

### 1. Confirmar resolução do plano de contas

Verificar qual arquivo de plano de contas o pipeline efetivamente carrega na máquina atual:

```bash
python main.py --client zeus --mes-ref 2026-01 2>&1 | grep -i "plano\|legado\|canônico"
```

Se o pipeline usar o arquivo em `~/Downloads/` (legado), avaliar se é necessário migrar esse arquivo para `cfg/clientes/zeus/plano_contas_cliente.csv` antes de prosseguir. Não renomear nem sobrescrever sem comparar o conteúdo com o atual `cfg/clientes/zeus/plano_contas_zeus.csv`.

### 2. Executar Zeus 2026-01 sem persistência

```bash
python main.py --client zeus --mes-ref 2026-01
```

Pré-requisitos:
- PDFs Zeus configurados em `cfg/clientes/zeus/config.yml` acessíveis (caminhos OneDrive válidos na máquina atual).
- `.env` com variáveis Supabase (mesmo sem `--save-supabase`, o pipeline tenta carregar `mapeamentos_cliente`).
- `~/Downloads/plano_contas_zeus.csv` existente (ou o canônico em `cfg/clientes/zeus/`).

### 3. Verificar categorias não mapeadas

Comparar `saida/zeus/2026-01/categorias_nao_mapeadas.csv` com o run anterior (40 categorias). Se houver diferença significativa, investigar antes de avançar.

### 4. Conferir estrutura e valores da DRE

Verificar se o DRE gerado tem a mesma estrutura de contas do run de junho. Atenção especial a INSS/IRRF e FGTS (nível 4 no plano atual).

### 5. Confrontar com apuração gerencial de referência

Obter a apuração gerencial aprovada para o período (responsável pelo cliente Zeus). Comparar linha a linha os valores do DRE gerado. Documentar aprovação ou divergências.

### 6. Somente após aprovação: avançar no produto

Só considerar o ciclo ETL → DRE validado quando a apuração gerencial for conferida. Só então é seguro avançar em: ajuste do plano Zixbe para novo formato, sincronização de colunas no Supabase, ou novas funcionalidades.

---

## Pendências técnicas

### Bloqueio para validação

| # | Descrição |
|---|---|
| B1 | Plano de contas fora do caminho canônico: `cfg/clientes/zeus/plano_contas_cliente.csv` e `cfg/clientes/zixbe/plano_contas_cliente.csv` não existem. Pipeline usa fallback `~/Downloads/`, que não é portável. |
| B2 | `plano_contas_zixbe.csv` está no formato antigo (6 colunas). Com o código atual, Zixbe não resolve nenhuma categoria via CSV — depende totalmente do Supabase. |
| B3 | Caminhos dos PDFs Zeus em `cfg/clientes/zeus/config.yml` são absolutos e apontam para o OneDrive da máquina original. Em novo ambiente, precisam ser atualizados antes de qualquer run. |
| B4 | DRE Zeus 2026-01 gerado com plano diferente do atual — não reproduzível diretamente. |

### Dívida técnica

| # | Descrição |
|---|---|
| D1 | `src/supabase_client.py → _upsert_plano_contas()` sincroniza apenas as 6 colunas básicas. Colunas `exibir_dre`, `auditoria_only` e `categoria_origem` não são persistidas no Supabase. |
| D2 | Tabelas `dre_mensal` e `resumo_conferencia` no Supabase usam `cliente` (TEXT), não `cliente_id` (UUID). Migração de schema necessária para alinhar com a chave `clientes.cliente_id`. |
| D3 | `cfg/clientes/zeus.yml` é um arquivo legado órfão (fora da pasta `zeus/`). O canônico é `cfg/clientes/zeus/config.yml`. O legado ainda funciona como fallback mas pode causar confusão. |
| D4 | Contas nível 4 no `plano_contas_zeus.csv` (INSS/IRRF id=76, FGTS id=77): o DRE processa apenas níveis 1–3. O tratamento gerencial correto dessas contas (nível 3 pai = "Encargos da Folha", id=75) precisa ser definido. |

### Evolução futura

| # | Descrição |
|---|---|
| E1 | Fluxo Lovable → Supabase → ETL: documentar e testar o ciclo completo de mapeamento pelo frontend. |
| E2 | Atualizar `plano_contas_zixbe.csv` para o formato de 9 colunas, preenchendo `categoria_origem` para que o mapeamento via CSV funcione sem depender do Supabase. |
| E3 | Suporte a novos clientes além de Zeus e Zixbe. |
| E4 | Testes automatizados cobrindo o pipeline end-to-end por cliente. |
