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
- Geração do DRE por rollup recursivo bottom-up (conta folha → agregadora), independente da profundidade da árvore — não há mais passo fixo por nível (ver "Evolução arquitetural" abaixo). Contas com múltiplas linhas de `categoria_origem` para o mesmo `id_conta` são deduplicadas antes do rollup do pai.
- Conta folha é determinada pela estrutura `id_conta`/`conta_pai` (ausência de filhos), não pelo valor de `nivel`. Agregador com `categoria_origem` preenchido é estrutura inválida, registrada como erro e ignorada no mapeamento.
- Validação DRE: checagem de quais `categoria_origem` dos lançamentos contribuem para o DRE (`exibir_dre=True`, conta folha) e quais seriam silenciosamente ignorados.
- Persistência no Supabase (opcional, flag `--save-supabase`).
- Hierarquia de resolução de caminhos: canônico (`cfg/clientes/`) → legado com aviso.

**As alterações de código mais recentes** (implementação do novo formato do plano de contas, `exibir_dre`, `auditoria_only`, `categoria_origem`) foram feitas em `etl/plano_contas.py`, `etl/pipeline.py` e `etl/mapeamentos.py`. **O ETL não foi reexecutado via `main.py` após essas alterações** — os outputs em `saida/` foram gerados com versões anteriores do código. A generalização de hierarquia variável (21/08/2026) está coberta por testes automatizados diretos sobre `gerar_dre()`/`ler_mapeamento_plano()` (ver "Evolução arquitetural"), mas também não foi exercitada via `main.py`.

---

## Evolução arquitetural — hierarquia variável no plano de contas (21/08/2026)

Nesta data, o motor de DRE foi generalizado para suportar profundidade variável na árvore do plano de contas. Motivação: a premissa "nível 3 = conta folha" já estava quebrada no próprio plano novo do Zeus — não só pelas contas de nível 4 (INSS/IRRF, FGTS), mas também por uma conta folha real em nível 2 (`Distribuição aos Sócios`, id=122), achado verificado diretamente no CSV antes de qualquer alteração de código.

**O que muda no modelo:**
- `nivel` deixa de determinar se uma conta recebe lançamentos diretamente. Passa a ser um atributo de exibição/ordenação/validação, não um insumo de cálculo.
- Conta folha é determinada estruturalmente: é folha toda conta cujo `id_conta` não aparece como `conta_pai` de nenhuma outra linha do plano — não depende de estar em nível 3, nem em nenhum nível específico.
- `categoria_origem` só é válido em conta folha. Uma conta agregadora (tem filhos) que também tenha `categoria_origem` preenchido é **estrutura inválida**: essa `categoria_origem` é ignorada na construção do mapeamento, e um erro explícito é registrado no log (`etl/plano_contas.py::_validar_plano()`). Não existe comportamento de "valor próprio + filhos" — essa opção foi deliberadamente descartada por decisão de produto.
- O rollup do DRE passa a ser recursivo e bottom-up pela árvore real (`conta_pai`/filhos), funcionando para qualquer profundidade, sem passo fixo `3→2→1` nem `4→3` hardcoded.
- Contas com múltiplas linhas físicas no CSV (mesmo `id_conta`, `categoria_origem` diferentes apontando para a mesma conta) são deduplicadas por `id_conta` antes do rollup do pai — uma conta com N linhas de `categoria_origem` conta uma vez só no agregador, nunca N vezes.
- Linhas de resultado por fórmula (`RECEITA LÍQUIDA`, `MARGEM DE CONTRIBUIÇÃO`, `RESULTADO OPERACIONAL`, `RESULTADO`) continuam sendo uma categoria separada, calculadas por casamento exato de `descricao` contra `_FORMULAS_RESULTADO`, independente de nível ou de estrutura de árvore — comportamento inalterado.

**Arquivos alterados:** `etl/dre.py` (rollup recursivo), `etl/plano_contas.py` (`ler_mapeamento_plano()` e `_validar_plano()` — folha estrutural em vez de nível 3, validação de agregador com `categoria_origem`), `etl/pipeline.py` (conjunto de folhas válidas em vez de `nivel3_validas`), `etl/mapeamentos.py` (parâmetro renomeado, mesma lógica). Nenhum CSV, YAML, `config.py`, schema do Supabase ou dado foi alterado nesta rodada.

**Testes:** 9 testes novos em `tests/test_dre.py`. Suíte completa: **36 testes passando** (27 pré-existentes de `test_zixbe.py` + 9 novos).

**Regressão confirmada — Zixbe e Zeus legado permanecem numericamente idênticos.** Os planos hoje ativos (Zixbe e o Zeus legado carregado via fallback) foram comparados linha a linha contra uma implementação de referência escrita de forma independente do código em produção, cobrindo a árvore inteira do plano — nenhuma conta mudou de valor.

**Três bugs silenciosos identificados e corrigidos no plano novo do Zeus** (`cfg/clientes/zeus/plano_contas_zeus.csv` — ainda não é o canônico, ver abaixo), comparando o motor de antes desta rodada com o motor novo, mesma entrada sintética:

| Bug | Antes | Depois |
|---|---|---|
| Dupla contagem — conta com múltiplas linhas de `categoria_origem` (`Salários`, id=72) | `DESPESAS COM PESSOAL` = 2000,00 (dobrado) | `DESPESAS COM PESSOAL` = 1000,00 |
| Folha nível 4 perdida (INSS/IRRF + FGTS → `Encargos da Folha`) | `Encargos da Folha` = 0,00 (silenciosamente ignorado) | `Encargos da Folha` = 500,00 |
| Folha real em nível 2 perdida (`Distribuição aos Sócios`) | `Distribuição aos Sócios` = 0,00 (silenciosamente ignorado) | `Distribuição aos Sócios` = 400,00 |

**O que NÃO mudou nesta rodada — continua exatamente como antes:**
- O plano novo do Zeus (`cfg/clientes/zeus/plano_contas_zeus.csv`) **ainda não é o plano canônico** carregado pelo pipeline. `resolver_plano_contas()` foi verificado explicitamente durante esta implementação e **continua** resolvendo para o fallback legado `~/Downloads/plano_contas_zeus.csv` (3 níveis, sem `categoria_origem`) — exatamente como já registrado em B1. Nenhum CSV foi promovido, renomeado ou movido.
- Nenhuma alteração em Supabase, schema, `.env`, ou dados de clientes.
- Nenhum commit ou push foi feito.
- `--save-supabase --overwrite` continua proibido até que a validação local seja concluída.

**Próximo passo:** decidir qual conteúdo do plano Zeus é a regra gerencial correta (ver "Origem do plano de contas" abaixo) e promovê-lo ao caminho canônico; em seguida, executar localmente (sem `--save-supabase`) a competência real do Zeus e confrontar com a apuração gerencial de referência — só então considerar a validação concluída.

---

## Evolução arquitetural — fórmulas declarativas e migração técnica Zeus (checkpoint)

Esta seção consolida tudo que aconteceu depois da hierarquia variável (seção acima) até o checkpoint técnico atual. Princípio seguido em toda a sequência: **mudar a arquitetura do motor sem alterar nenhuma regra gerencial nem resultado numérico existente.**

### Auditoria Supabase — origem de `Vendas a Prazo`

Antes de qualquer alteração no CSV, foi feita uma auditoria 100% read-only do Supabase para responder de onde vinha o mapeamento que produzia `RECEITA BRUTA = R$ 666.458,20` no run histórico. Achados:
- `mapeamentos_cliente` da Zeus tem só **3 registros**, todos ativos, sem duplicidade, sem destino inválido, sem problema de nomenclatura.
- `Vendas a Prazo → Vendas de Produtos` é um deles — a `descricao_conta` bate exatamente com `id_conta=3` do plano novo, cuja `categoria_origem` no CSV estava vazia. **Foi o Supabase, não o CSV, que fechava a Receita Bruta no run histórico.**
- Reconstituição em memória (CSV novo + esses 3 mapeamentos do Supabase + PDFs reais de 2026-01, sem gravar nada) reproduziu `RECEITA BRUTA = R$ 666.458,20` exatamente.

### Saneamento do plano Zeus novo (`cfg/clientes/zeus/plano_contas_zeus.csv`)

Decisão de arquitetura tomada: **CSV é o baseline completo do cliente; Supabase é só overrides pontuais.** Um mapeamento estrutural e permanente como `Vendas a Prazo` não deveria depender só do Supabase. Três correções mínimas, autorizadas e aplicadas:
1. `id_conta=3` (`Vendas de Produtos`) ganhou `categoria_origem=Vendas a Prazo`.
2. Typo `DEPESAS ADMINISTRATIVAS` → `DESPESAS ADMINISTRATIVAS` (`id=46`).
3. Dois espaços non-breaking (U+00A0) normalizados para espaço comum (`Aluguel, Condomínio e IPTU`, `Material de Limpeza e Cozinha`).

Varredura completa do arquivo não achou nenhum outro caractere Unicode incomum nem espaço de borda residual. Teste CSV-only (sem Supabase) após o saneamento reproduziu os mesmos 9 valores de referência, byte a byte — **o plano Zeus novo é autossuficiente via CSV**, sem depender do Supabase para nada essencial.

### Memória de homologação e achado do grupo `IMPOSTOS`

Foi gerada uma memória detalhada (conta a conta, com hierarquia completa) da DRE Zeus 2026-01 para revisão gerencial — entregue como artefato, não como arquivo do repositório. Reconciliação confirmou Receita Líquida, Margem, Resultado Operacional e Resultado batendo com os subtotais somados manualmente.

Achado central: **`IMPOSTOS` (ICMS + PIS + COFINS = R$ 59.167,42 em 2026-01) é um grupo de nível 1 separado que não é referenciado por nenhuma fórmula de resultado** — a Receita Líquida usa só `RECEITA BRUTA − DEDUÇÕES DA RECEITA`. O valor aparece no relatório mas não reduz nenhum subtotal de resultado. Não é um bug do motor (o motor agrega `IMPOSTOS` corretamente); é evidência de que o motor não conseguia executar livremente uma composição de Receita Líquida diferente da fixada em Python. Não classificado como erro gerencial — decisão pendente para a Skill/metodologia.

### Diagnóstico arquitetural — hardcodes gerenciais no motor

Levantamento completo (leitura de `etl/dre.py`, `etl/plano_contas.py`, `etl/pipeline.py`, `etl/mapeamentos.py`, `config.py`, greps exaustivos) do que estava fixado em Python e deveria, pelo princípio *Skill → CSV → motor executa*, ser declarável pelo plano:

| Achado | Classificação |
|---|---|
| `_FORMULAS_RESULTADO` (12 nomes de grupo hardcoded, topologia aritmética fixa) | Migrar para o plano — confirmado pelo caso `IMPOSTOS` |
| Aviso de "nível 3 sem `categoria_origem`" restrito a `nivel==3` | Resolvido nesta sequência (ver abaixo) |
| `mapeamento_categorias` manual em `config.py` (mapeamento vivendo em `.py`, exige commit) | Continua dívida (ver Pendências) |
| `ler_plano_contas()` legado, `nivel==3` hardcoded, zero chamadores | Código morto — continua dívida |
| Folha = sem filhos; sinal aplicado uma vez; validação de agregador+`categoria_origem` | Permanece no motor — são regras estruturais da árvore, não decisões gerenciais |
| Dispatch de processador por cliente; parsing de coordenadas x do PDF Wingraph | Permanece no motor — integração com formato de origem, não modelagem de DRE |

### Infraestrutura declarativa implementada

Duas colunas novas, opcionais, retrocompatíveis: `tipo_conta` (`calculada` | default `estrutural`) e `formula` (sintaxe `[+-]coeficiente*id_conta`, termos separados por espaço, **sempre por `id_conta`, nunca por `descricao`**). Três papéis mutuamente exclusivos — folha estrutural, agregadora estrutural, conta calculada — validados na carga do plano com erro explícito (nunca `0.0` silencioso) para: ID inexistente, fórmula vazia, sintaxe inválida, autorreferência, ciclo entre calculadas, calculada com filhos, calculada com `categoria_origem`. Resolução de dependência por recursão memoizada (mesmo padrão do rollup estrutural), não pela ordem física das linhas.

Semântica explícita e documentada: a fórmula declarativa opera sobre `valor_base` **natural** (pré-sinal); o `sinal` da própria conta calculada é aplicado depois, uniformemente com folha/agregadora. O fallback legado (`_FORMULAS_RESULTADO`) continua operando sobre valores **já exibidos** (pós-sinal) — preservado assim, pixel a pixel, para não alterar nenhum plano existente. As duas semânticas coexistem sem se misturar: o fallback só é consultado para uma `descricao` se nenhuma linha com esse nome tiver sido declarada `calculada`.

Aproveitando a mudança, o aviso de "conta sem `categoria_origem`" deixou de depender de `nivel==3` — passou a ser "folha estrutural (não calculada, não legada) sem `categoria_origem`", entendendo semanticamente os três papéis, sem assumir que algum nível específico recebe lançamento.

**Testes:** `tests/test_formulas_calculadas.py`, 22 testes novos (regressão sem colunas novas, conta calculada simples/soma/subtração, dependência entre calculadas, referência em nível 2/3/4, rename de `descricao` sem quebrar fórmula, ordem física irrelevante, todos os 7 casos de erro).

### Migração técnica das 4 fórmulas de resultado da Zeus

Com a infraestrutura aprovada, as 4 linhas de resultado do plano Zeus novo (`RECEITA LÍQUIDA` id=31, `MARGEM DE CONTRIBUIÇÃO` id=44, `RESULTADO OPERACIONAL` id=90, `RESULTADO` id=110) foram migradas de `_FORMULAS_RESULTADO` para `tipo_conta=calculada` com fórmula por `id_conta` — **sem alterar nenhuma regra gerencial**, só a representação técnica:

```
id=31  formula="+1*1 +1*23"     (RECEITA BRUTA + DEDUÇÕES DA RECEITA, natural)
id=44  formula="+1*31 +1*32"    (RECEITA LÍQUIDA + CUSTO VARIÁVEL, natural — depende de outra calculada)
id=90  formula="+1*44 +1*45"    (MARGEM + DESPESAS FIXAS, natural)
id=110 formula="+1*90 +1*111"   (RESULTADO OPERACIONAL + INVESTIMENTOS, natural)
```

Coeficientes derivados pela regra `coeficiente = operador_legado × sinal(calculada) × sinal(referenciada)` — todos `+1`, porque toda conta com operador `−1` no dict legado também tem `sinal=−1` no plano (a inversão dupla cancela). Verificado à mão, termo a termo, contra os valores reais, antes de escrever qualquer coisa no CSV.

**Achado documentado como evidência de que fórmulas declarativas com validação explícita são mais seguras**: a fórmula legada de `RESULTADO` referencia `RECEITAS FINANCEIRAS`, `DESPESAS FINANCEIRAS`, `PASSIVO` e `DIVISÃO DE LUCROS` — **nenhum desses nomes existe** como `descricao` em nenhum lugar do plano Zeus novo. O fallback já os tratava como zero silenciosamente via `.get(dep, 0.0)`, não por ausência de movimento no mês, mas porque os nomes simplesmente não existem neste plano. A fórmula declarativa não inventou contas substitutas — referenciou só os `id_conta` que de fato existem (90 e 111), preservando exatamente o comportamento efetivo de hoje. Isso **não é classificado como erro gerencial da Zeus** — é uma decisão que pertence ao plano/Skill numa etapa futura. O ponto arquitetural é que, com o mecanismo declarativo, uma referência a um `id_conta` inexistente teria sido um **erro explícito na carga do plano**, em vez de um zero silencioso — exatamente o tipo de falha que motivou toda essa sequência de mudanças.

**Equivalência numérica confirmada** (Zeus 2026-01, CSV-only, sem Supabase, plano migrado): os 9 valores de referência (Receita Bruta 666.458,20 … Resultado 293.317,01) bateram exatamente, diferença 0,00 em todos. **Prova de que o fallback não participou**: `_FORMULAS_RESULTADO` foi esvaziado em memória (só nesta verificação, nenhum arquivo alterado) e os 4 valores permaneceram idênticos — confirmando que vieram inteiramente do mecanismo declarativo.

**Suíte completa: 58 testes passando** (36 da hierarquia variável + 22 de fórmulas declarativas). Zixbe e Zeus legado (fixtures congeladas, sem `tipo_conta`/`formula`) continuam resolvidos pelo fallback; só a Zeus real foi migrada.

### O que NÃO mudou nesta sequência

Nenhuma regra gerencial da Zeus foi alterada — `IMPOSTOS` continua fora da Receita Líquida, nenhuma categoria foi reclassificada, nenhuma `categoria_origem` além de `Vendas a Prazo` foi tocada. `plano_contas_zeus.csv` continua fora do caminho canônico. Nenhuma escrita no Supabase, nenhuma alteração de schema.

---

## Origem do plano de contas — processo "Gerador de Plano de Contas"

Informação recuperada do antigo processo interno chamado "Gerador de Plano de Contas". Registrada aqui porque explica a origem das diferenças encontradas no plano Zeus (seção abaixo).

O "Gerador de Plano de Contas" **não é um software independente** — é um processo assistido por IA para implantação/configuração de clientes do Software DRE. Sua saída (o CSV do plano de contas) alimenta diretamente o ETL. Por isso, qualquer evolução de schema que esse processo produza (ex.: número de níveis) precisa ser compatível com o motor do DRE **antes** de ser considerada padrão — a saída do gerador não é, por si só, fonte de verdade validada.

O processo trabalha com três elementos:

- **Categoria de Origem** — categoria como ela chega do ERP do cliente.
- **Categoria Destino** — conta gerencial para a qual a categoria de origem é mapeada.
- **Estrutura DRE** — hierarquia gerencial (níveis) na qual a conta destino está inserida.

É um processo *human-in-the-loop*:
1. analisar as categorias de origem e a estrutura;
2. propor um de/para (origem → destino) e uma hierarquia;
3. marcar itens incertos como `A CLASSIFICAR`, em vez de assumir uma classificação;
4. apresentar o resultado para validação humana;
5. só então gerar/persistir o plano de contas definitivo.

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

**Ambiguidade de fonte do plano Zeus:** existem dois arquivos com conteúdos diferentes.

| Arquivo | Data | Formato | Observação |
|---|---|---|---|
| `~/Downloads/plano_contas_zeus.csv` | 18/05, 17:08 | 6 colunas (antigo) | Arquivo efetivamente usado pelo pipeline hoje (fallback legado) |
| `cfg/clientes/zeus/plano_contas_zeus.csv` | 06/07, 14:43 | 9 colunas (novo) | Presente no repositório, mas fora do caminho de resolução — nunca carregado pelo pipeline |

**Origem de cada versão** (ver seção "Origem do plano de contas — processo 'Gerador de Plano de Contas'" acima):
- A versão antiga (`~/Downloads/plano_contas_zeus.csv`, 6 colunas) foi construída a partir de conteúdo textual da máscara DRE do Zeus, sem preservar informação visual da planilha, e trabalha essencialmente com até 3 níveis.
- A versão nova (`cfg/clientes/zeus/plano_contas_zeus.csv`, 9 colunas) foi gerada posteriormente a partir da planilha XLSX da máscara DRE do Zeus, com a hierarquia inferida também pela formatação/indentação visual da planilha. Foi dessa interpretação visual que surgiram as contas de nível 4, incluindo INSS/IRRF e FGTS como filhas de "Encargos da Folha".

O arquivo do repositório é mais recente e usa o novo formato com `exibir_dre`, `auditoria_only` e `categoria_origem`. **Não há evidência de que essa versão de 9 colunas tenha sido homologada como nova fonte de verdade do Software DRE.** Ela representa uma evolução/proposta do processo de geração do plano, mas não houve reconciliação explícita com o motor do DRE: o arquivo nunca foi colocado no caminho canônico (`plano_contas_cliente.csv`) nem referenciado de nenhuma outra forma pelo pipeline.

**Consequência arquitetural (nível 4):** o motor DRE (`etl/dre.py`) processa apenas níveis 1–3; nível 4 é silenciosamente ignorado no rollup. Isso **não deve ser tratado como bug do motor, nem como prova de que o motor deve passar a suportar nível 4** — é uma decisão gerencial/estrutural ainda pendente, com duas alternativas em aberto:
1. o plano Zeus deve realmente possuir quatro níveis, e o motor do DRE precisa evoluir para suportá-los; ou
2. a hierarquia interpretada a partir do XLSX deve ser normalizada para os três níveis atualmente suportados pelo motor.

Essa decisão precisa ocorrer **antes** de promover `plano_contas_zeus.csv` para `plano_contas_cliente.csv` e **antes** do próximo teste Zeus 2026-01. Não assumir que nenhuma das versões existentes está correta sem essa decisão.

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
- **Código atual sem regressões via execução real**: as alterações de plano de contas de 9 colunas (`exibir_dre`/`auditoria_only`/`categoria_origem`) e, mais recentemente, a generalização de hierarquia variável (21/08/2026) estão cobertas por testes automatizados (36 testes, incluindo regressão contra uma referência independente para os planos hoje ativos — ver "Evolução arquitetural" acima). Isso **não é o mesmo** que uma execução real via `main.py`/`--save-supabase`: os testes chamam `gerar_dre()`/`ler_mapeamento_plano()` diretamente, não o pipeline completo com um cliente real.
- **Mapeamento Zixbe via CSV funcional**: com o plano antigo (6 colunas), `ler_mapeamento_plano()` retorna `{}`. O mapeamento depende inteiramente do Supabase ou de config manual, e o estado atual do `mapeamentos_cliente` no Supabase não está documentado.
- **Contas de nível 4 Zeus tratadas corretamente PELO MOTOR**: resolvido em 21/08/2026 — o motor do DRE agora suporta profundidade variável e trata `Encargos da Folha`/INSS-IRRF/FGTS corretamente (comprovado por teste automatizado, ver "Evolução arquitetural" acima). O que permanece **não confirmado** é diferente: (a) se essa estrutura de 4 níveis é o plano de contas gerencialmente correto para o Zeus — a decisão entre o conteúdo do arquivo legado e o do repositório continua pendente (ver "Origem do plano de contas"); e (b) a validação gerencial real do DRE Zeus 2026-01, que não foi feita com nenhuma versão do plano.
- **Composição da Receita Líquida (e demais linhas de resultado) da Zeus validada gerencialmente**: as 4 fórmulas foram migradas para o mecanismo declarativo reproduzindo exatamente o comportamento anterior (checkpoint técnico) — isso **não é** uma revisão gerencial. Em particular, `IMPOSTOS` (R$ 59.167,42 em 2026-01) continua fora da Receita Líquida, e a fórmula de `RESULTADO` continua sem termos correspondentes a `RECEITAS FINANCEIRAS`/`DESPESAS FINANCEIRAS`/`PASSIVO`/`DIVISÃO DE LUCROS` (nomes que não existem no plano). Nenhuma dessas composições foi revisada pela Skill/metodologia de implantação — é o próximo passo, não uma correção automática deste checkpoint.
- **Estado do Supabase após investigação de integridade**: houve investigação anterior relacionada ao isolamento de dados entre clientes, resultando nas proteções defensivas descritas na seção de pendências. Não há evidência suficiente para confirmar a causa raiz exata, quais dados foram afetados ou se o estado atual das tabelas por cliente e competência está íntegro.

---

## Ponto de retomada

**Atualização deste checkpoint**: os passos 1–2 abaixo já não são mais o gargalo técnico que eram quando esta seção foi escrita — o plano `cfg/clientes/zeus/plano_contas_zeus.csv` já foi saneado, comprovado autossuficiente via CSV (sem depender do Supabase) e migrado para fórmulas declarativas reproduzindo exatamente o comportamento anterior. O próximo passo real é **semântico, não técnico**: a Skill/metodologia de implantação revisar o conteúdo do plano (a começar pela composição de `IMPOSTOS` na Receita Líquida) e só então decidir sobre promoção ao caminho canônico. Os passos originais ficam abaixo como registro histórico da sequência que levou até aqui.

O próximo passo é tornar o processamento Zeus reproduzível no ambiente atual e validar o DRE contra uma apuração gerencial de referência. A sequência recomendada:

### 1. Resolver a ambiguidade do plano de contas Zeus

Existem dois arquivos com estruturas diferentes (ver seção "Último estado conhecido — Zeus"). Antes de qualquer run:

1. Comparar o conteúdo dos dois arquivos (Downloads vs. repositório).
2. Decidir qual versão representa a regra gerencial correta para o Zeus. Essa decisão **não depende mais de limitação técnica do motor** — desde 21/08/2026 o motor suporta qualquer profundidade (ver "Evolução arquitetural"). É puramente uma decisão sobre qual estrutura reflete a realidade contábil do cliente (ver "Origem do plano de contas").
3. Colocar o arquivo escolhido no caminho canônico (`cfg/clientes/zeus/plano_contas_cliente.csv`).

Não renomear nem sobrescrever sem esta decisão. Confirmar qual arquivo o pipeline carrega:

```bash
python main.py --client zeus --mes-ref 2026-01 2>&1 | grep -i "plano\|legado\|canônico"
```

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
| D4 | **[Resolvido no motor em 21/08/2026; plano saneado e autossuficiente desde então]** Contas nível 4 no `cfg/clientes/zeus/plano_contas_zeus.csv` (INSS/IRRF id=76, FGTS id=77): o motor do DRE suporta profundidade variável. O plano foi saneado (`Vendas a Prazo` incorporado ao CSV, typo e whitespace corrigidos) e comprovado autossuficiente via CSV, sem depender do Supabase. As 4 linhas de resultado já foram migradas para fórmula declarativa (`tipo_conta=calculada`), reproduzindo exatamente o comportamento anterior. O que resta é puramente gerencial: confirmar se a estrutura de 4 níveis é a regra correta, decidir a composição de `IMPOSTOS` na Receita Líquida, e só então promover o arquivo ao caminho canônico — nenhuma dessas decisões foi tomada. |
| D5 | **Integridade Supabase — investigação anterior**: o código contém proteções defensivas extensas em `src/supabase_client.py` (reverse-lookup de `cliente_id`, verificação de `upload_ids` antes de deletar, validação pós-insert, GROUP BY final) resultantes de investigação sobre isolamento de dados entre clientes. A causa raiz exata, os clientes/dados afetados e o estado atual das tabelas após essa investigação **não estão documentados**. Os CHECKPOINTs em `pipeline.py`/`main.py` (prints rotulados "CLIENTE RECEBIDO NO MAIN/PIPELINE/SUPABASE" e `RAW res.data`) parecem resíduos de debugging desse período — candidatos a remoção ou conversão para `logger.debug()`, mas a decisão não foi tomada. As validações de integridade (abort em divergência de `cliente_id`/`upload_id`) devem ser consideradas proteções permanentes. Antes de executar `--save-supabase --overwrite`, conferir o estado atual das tabelas por cliente e competência diretamente no Supabase. |
| D6 | `exibir_dre=False` filtra a linha **antes** dela chegar a `gerar_dre()` (em `pipeline.py`) — então uma conta calculada não pode hoje referenciar (por `id_conta`) uma conta marcada `exibir_dre=False`, mesmo que fosse estruturalmente válido (a referência falharia por "id não encontrado no plano fornecido"). Não bloqueia nada hoje porque nenhuma fórmula declarada referencia esse tipo de conta, mas é uma limitação conhecida do design atual. |
| D7 | O fallback legado (`_FORMULAS_RESULTADO`, opera sobre valor **exibido**/pós-sinal) e o mecanismo declarativo novo (opera sobre `valor_base` **natural**/pré-sinal) têm semânticas internas diferentes de sinal, coexistindo deliberadamente sem se misturar — produzem regressão numérica idêntica para os planos migrados, mas uma conta calculada declarativa não pode hoje referenciar (por `id_conta`) um valor que só existe por causa do fallback legado (a resolução de dependência não unifica os dois grafos). Não testado nem necessário até hoje, porque nenhum plano mistura os dois mecanismos. |
| D8 | `mapeamento_categorias` manual em `config.py` (dict hardcoded em `.py`, vazio para os dois clientes hoje) é mapeamento de dado vivendo em código-fonte — redundante com CSV (`categoria_origem`) e Supabase (`mapeamentos_cliente`), que já cobrem esse papel. Nenhuma migração de dado necessária (está vazio); decisão pendente é se o mecanismo continua existindo. |
| D9 | `etl/plano_contas.py::ler_plano_contas()` (função legada, `nivel==3` hardcoded) não tem nenhum chamador no código — confirmado por busca exaustiva. Código morto, candidato a remoção. |

### Evolução futura

| # | Descrição |
|---|---|
| E1 | Fluxo Lovable → Supabase → ETL: documentar e testar o ciclo completo de mapeamento pelo frontend. |
| E2 | Atualizar `plano_contas_zixbe.csv` para o formato de 9 colunas, preenchendo `categoria_origem` para que o mapeamento via CSV funcione sem depender do Supabase. |
| E3 | Suporte a novos clientes além de Zeus e Zixbe. |
| E4 | Testes automatizados cobrindo o pipeline end-to-end por cliente. |
