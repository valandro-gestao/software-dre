# ETL Financeiro — DRE por cliente

Pipeline Python que lê lançamentos financeiros de fontes ERP (Excel ou PDF) e gera o Demonstrativo de Resultado do Exercício (DRE) mensal em CSV, com integração opcional ao Supabase.

---

## Objetivo

Transformar extratos brutos do ERP em um DRE estruturado por plano de contas gerencial, permitindo:

- conferência matemática (total ERP × total ETL);
- identificação de categorias não mapeadas;
- persistência no Supabase para visualização no Lovable.

---

## Pré-requisitos

- Python 3.10+
- Dependências: `pip install -r requirements.txt`
  - pandas, openpyxl, supabase, python-dotenv, pdfplumber, PyYAML
- `.env` com as variáveis Supabase (necessário apenas para `--save-supabase`):
  ```
  SUPABASE_URL=...
  SUPABASE_SERVICE_ROLE_KEY=...
  ```

---

## Estrutura de diretórios

```
conta_azul_etl/
├── cfg/
│   └── clientes/
│       └── {cliente}/
│           ├── config.yml               ← mapeamento de PDFs por mês (clientes PDF)
│           └── plano_contas_cliente.csv ← plano de contas (nome canônico)
├── data/
│   └── raw/
│       └── {cliente}/
│           └── {mes_ref}/               ← arquivos originais (PDF, XLS)
├── saida/
│   └── {cliente}/
│       └── {mes_ref}/                   ← CSVs gerados pelo ETL
├── etl/
│   ├── pipeline.py    ← orquestra o fluxo completo
│   ├── plano_contas.py
│   ├── mapeamentos.py
│   ├── dre.py
│   ├── paths.py
│   └── clients/
│       ├── zixbe.py   ← processador Excel (Conta Azul)
│       └── zeus.py    ← processador PDF (Wingraph)
├── src/
│   └── supabase_client.py
├── config.py          ← registro de clientes
└── main.py            ← ponto de entrada CLI
```

---

## Como executar

### Cliente Excel (ex.: Zixbe)

```bash
python main.py data/raw/zixbe/2026-04/relatorio.xlsx --client zixbe --mes-ref 2026-04
```

### Cliente PDF (ex.: Zeus)

Os PDFs são configurados em `cfg/clientes/zeus/config.yml` por mês.

```bash
python main.py --client zeus --mes-ref 2026-01
```

### Flags comuns

| Flag | Descrição |
|---|---|
| `--output-dir DIR` | Diretório raiz de saída (padrão: `./saida`) |
| `--save-supabase` | Persiste no Supabase (requer `.env`) |
| `--overwrite` | Sobrescreve dados existentes no Supabase para o cliente/mês |
| `--mes-ref AAAA-MM` | Mês de referência (obrigatório com `--save-supabase`) |

---

## CSVs de saída

Gerados em `saida/{cliente}/{mes_ref}/`:

| Arquivo | Conteúdo |
|---|---|
| `lancamentos_normalizados.csv` | Todos os lançamentos com categoria mapeada |
| `dre_mensal.csv` | DRE estruturado por conta folha/agregadora (profundidade variável) e linhas de resultado |
| `resumo_conferencia.csv` | Total ERP × Total ETL, diferença e contagens |
| `resumo_auditoria.csv` | Por empresa/tipo (apenas Zeus multi-CNPJ) |
| `inconsistencias.csv` | Lançamentos com problemas de parsing |
| `categorias_nao_mapeadas.csv` | Categorias sem correspondência no plano de contas |

---

## Plano de contas

### Formato atualmente suportado pelo código

Separador `;` ou `,` (detectado automaticamente). Colunas:

```
id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem;tipo_conta;formula
```

| Coluna | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `id_conta` | int | Sim | Identificador único da conta |
| `nivel` | int | Sim | Nível hierárquico, usado só para exibição/ordenação/validação — **não** determina se a conta é folha nem limita a profundidade da árvore (ver "Regras") |
| `conta_pai` | int | Sim | `id_conta` do nível acima (vazio no nível 1) |
| `ordem` | int | Sim | Ordem de exibição no DRE |
| `descricao` | str | Sim | Nome gerencial da conta |
| `sinal` | int (+1/-1) | Sim | Inverte o sinal para exibição (+receitas / −despesas) |
| `exibir_dre` | bool | Não¹ | Inclui a linha no DRE (padrão: `true`) |
| `auditoria_only` | bool | Não¹ | Categoria de conferência, mapeada mas fora do DRE (padrão: `false`) |
| `categoria_origem` | str | Não¹ | Nome exato da categoria no ERP; chave do mapeamento automático. Só é válido em conta folha (ver "Regras") |
| `tipo_conta` | str | Não¹ | `calculada` marca uma conta cujo valor vem exclusivamente da fórmula em `formula` — nunca de rollup nem de lançamentos. Qualquer outro valor (ou coluna ausente) é tratado como `estrutural` (padrão) |
| `formula` | str | Não¹ | Obrigatória quando `tipo_conta=calculada`. Ver "Contas calculadas — fórmulas declarativas" abaixo |

¹ Colunas opcionais com retrocompatibilidade: CSVs no formato antigo (6 colunas) funcionam com os valores padrão.

### Regras

Toda conta do plano tem exatamente um de três papéis, mutuamente exclusivos:

1. **Folha estrutural** — `id_conta` não aparece como `conta_pai` de nenhuma outra linha, e não é `tipo_conta=calculada`. Recebe lançamentos: `valor_base` = soma dos lançamentos cujo `categoria_origem` (já mapeado) bate com sua `descricao`.
2. **Agregadora estrutural** — é `conta_pai` de outra(s) linha(s). `valor_base` = soma recursiva dos filhos.
3. **Conta calculada** — `tipo_conta=calculada`. `valor_base` vem exclusivamente da `formula` declarada; nunca de rollup nem de lançamentos. **Não pode ter filhos nem `categoria_origem` preenchida** — ambos são erro explícito na carga do plano.

Papel é determinado pela estrutura `id_conta`/`conta_pai`/`tipo_conta` — nunca pelo valor de `nivel` (que é só exibição/ordenação) nem pelo nome (`descricao`). A árvore pode ter qualquer profundidade.

- Só conta folha pode ter `categoria_origem` preenchido. Uma conta agregadora com `categoria_origem` preenchido é **estrutura inválida**: ignorada na construção do mapeamento, erro registrado no log — não existe comportamento de "valor próprio + filhos".
- Múltiplas linhas com o mesmo `id_conta` podem ter `categoria_origem` diferentes apontando para a mesma conta, em qualquer nível. O rollup soma cada `id_conta` uma única vez no agregador-pai — sem dupla contagem.
- `auditoria_only=true`: categoria mapeada para evitar falso positivo em `categorias_nao_mapeadas.csv`, mas excluída do DRE.
- Booleanos aceitos: `Sim/Não`, `true/false`, `1/0`, `sim/não` (case-insensitive).

### Contas calculadas — fórmulas declarativas

`formula` é uma lista de termos separados por **espaço** (nunca `;`, que colidiria com o delimitador do CSV), cada termo no formato:

```
[+-]coeficiente*id_conta
```

Exemplo: `"+1*1 -1*23"` soma a conta `id_conta=1` e subtrai a conta `id_conta=23`. **Referencia sempre `id_conta`, nunca `descricao`** — `descricao` é atributo de apresentação e pode ser renomeado sem quebrar nenhuma fórmula. Não é um parser de expressão genérico (sem precedência, sem parênteses, sem `eval`) — deliberadamente restrito a soma/subtração de termos coeficiente×conta.

Semântica de cálculo: a fórmula opera sobre o `valor_base` **natural** (pré-sinal) das contas referenciadas — o mesmo espaço onde folha/agregadora já operam. O `sinal` da própria conta calculada é aplicado depois, uma única vez, exatamente como para qualquer outra conta. Isso significa que o coeficiente de um termo que referencia uma conta com `sinal=-1` normalmente é `+1`, não `-1` — ver exemplo comentado em `tests/test_formulas_calculadas.py::TestSemanticaSinalNaturalVsExibido`.

Uma conta calculada pode referenciar folha, agregadora ou outra conta calculada (dependência entre calculadas), em qualquer nível e em qualquer ordem física no CSV — a ordem de dependência é resolvida por recursão memoizada com detecção de ciclo, nunca pela ordem das linhas.

Validado na carga do plano, com erro explícito (nunca falha silenciosa com `0.0`):
- referência a `id_conta` inexistente;
- `tipo_conta=calculada` sem `formula` (ou vazia);
- sintaxe de `formula` inválida;
- autorreferência direta;
- dependência circular entre contas calculadas;
- conta calculada que também é agregadora (tem filhos);
- conta calculada com `categoria_origem` preenchida.

**Compatibilidade legada:** nenhum plano é obrigado a usar `tipo_conta`/`formula`. Uma descrição que exista em `etl/dre.py::_FORMULAS_RESULTADO` (`RECEITA LÍQUIDA`, `MARGEM DE CONTRIBUIÇÃO`, `RESULTADO OPERACIONAL`, `RESULTADO`) e não tenha sido explicitamente declarada `calculada` continua sendo resolvida pelo mecanismo antigo (por nome, sobre valores já exibidos) — um fallback mantido só para planos que ainda não migraram. Um plano não depende do nome de nenhuma conta para ter comportamento correto assim que declara suas próprias fórmulas.

### Nome do arquivo

O pipeline busca o plano nesta ordem:

1. `cfg/clientes/{cliente}/plano_contas_cliente.csv` (caminho canônico)
2. Caminho legado configurado em `config.py` → `plano_contas_legado` (exibe aviso)
3. Sem plano → mapeamento desabilitado (todas as categorias vão para `categorias_nao_mapeadas.csv`)

---

## Resolução de mapeamento de categorias

Três fontes são combinadas em ordem crescente de prioridade:

```
plano_contas CSV  ←  mapeamentos_cliente (Supabase)  ←  config.py manual
```

1. **plano_contas CSV** — fonte principal. Coluna `categoria_origem` → `descricao` (nível 3).
2. **mapeamentos_cliente (Supabase)** — overrides configurados pelo usuário no Lovable. Útil para categorias que a coluna `categoria_origem` não cobre.
3. **config.py → `mapeamento_categorias`** — sobrescritas manuais hardcoded; máxima prioridade.

Categorias sem nenhum mapeamento são registradas em `categorias_nao_mapeadas.csv`.

---

## Algoritmo do DRE

1. **Conta folha** (sem filhos, não calculada, `exibir_dre=True`) — `valor_base` = soma dos lançamentos cujo `categoria_origem` bate com sua `descricao`.
2. **Conta agregadora** (tem filhos) — `valor_base` = rollup recursivo bottom-up dos filhos, deduplicado por `id_conta`. Qualquer profundidade, sem passo fixo por nível.
3. **Conta calculada** (`tipo_conta=calculada`) — `valor_base` = combinação declarada em `formula`, resolvida recursivamente (pode referenciar folha, agregadora ou outra calculada). Ver "Contas calculadas — fórmulas declarativas" acima.
4. **Sinal** — aplicado uma única vez, sobre o `valor_base` já consolidado de cada conta (folha, agregadora **ou calculada**, por igual), multiplicando pelo campo `sinal` do plano.
5. **Fallback legado** — uma descrição presente em `_FORMULAS_RESULTADO` (`RECEITA LÍQUIDA`, `MARGEM DE CONTRIBUIÇÃO`, `RESULTADO OPERACIONAL`, `RESULTADO`) que **não** tenha sido declarada `calculada` em nenhuma linha do plano continua calculada pelo mecanismo antigo, por casamento de nome sobre valores já exibidos — mantido só para planos ainda não migrados para o mecanismo declarativo.

Os nomes das linhas de resultado do fallback legado devem existir exatamente como `descricao` no plano de contas; contas calculadas declarativas não têm essa restrição (referenciam por `id_conta`).

Exemplo real de profundidade variável: no plano do Zeus, `Encargos da Folha` (nível 3) é agregadora de `INSS / IRRF` e `FGTS` (nível 4, folhas) — o rollup soma essas duas folhas em `Encargos da Folha` normalmente, sem tratamento especial por nível.

---

## Clientes registrados

### Zixbe

- Fonte: Excel exportado do Conta Azul (visão de competência).
- Colunas esperadas no XLS: `Data de competência`, `Descrição`, `Nome do fornecedor/cliente`, `Valor (R$)`, colunas dinâmicas `Categoria N`, `Valor na Categoria N`.
- Plano atual: formato antigo (6 colunas, sem `categoria_origem`). O mapeamento via CSV retorna `{}` neste estado — as categorias são resolvidas pelo Supabase ou marcadas como não mapeadas.

### Zeus

- Fonte: PDFs Wingraph (AP = contas a pagar, AR = contas a receber), multi-CNPJ.
- PDFs configurados por mês em `cfg/clientes/zeus/config.yml`.
- Parsing posicional de coordenadas x pelo `etl/clients/zeus_reader.py`.
- O campo `empresa_origem` de cada PDF é propagado nos lançamentos para auditoria, mas não afeta o DRE.
- O plano em `cfg/clientes/zeus/plano_contas_zeus.csv` usa o formato novo (11 colunas) e já tem suas 4 linhas de resultado (`RECEITA LÍQUIDA`, `MARGEM DE CONTRIBUIÇÃO`, `RESULTADO OPERACIONAL`, `RESULTADO`) declaradas como `tipo_conta=calculada` com fórmula por `id_conta` — migração técnica que reproduz exatamente o comportamento do fallback legado (ver `ESTADO.md`). Esse arquivo ainda **não** está no caminho canônico (`plano_contas_cliente.csv`) — `resolver_plano_contas()` continua caindo no fallback legado de `~/Downloads/`.

---

## Supabase e Lovable

O Supabase serve como backend do Lovable (frontend de gestão). A integração é ativada com `--save-supabase`.

Tabelas relevantes:

| Tabela | Papel |
|---|---|
| `clientes` | Cadastro de clientes (chave: `cliente_id` UUID) |
| `lancamentos` | Lançamentos normalizados por cliente/mês |
| `dre_mensal` | Linhas do DRE por cliente/mês |
| `plano_contas` | Plano de contas sincronizado (6 colunas básicas — ver pendências em ESTADO.md) |
| `mapeamentos_cliente` | Overrides de mapeamento criados no Lovable |
| `resumo_conferencia` | Totais e contagens por run |

O Supabase usa `service_role_key`, que bypassa RLS.

---

## Como adicionar um novo cliente

1. Crie `cfg/clientes/{nome_lower}/plano_contas_cliente.csv` no formato suportado.
2. Se o cliente usar PDFs, crie `cfg/clientes/{nome_lower}/config.yml` com os PDFs por mês.
3. Adicione a entrada em `config.py → CLIENTES`.
4. Implemente `etl/clients/{nome_lower}.py` herdando de `ProcessadorBase` (veja `etl/clients/base.py`).
5. Registre o processador em `etl/pipeline.py → _PROCESSADORES`.

---

## Observações de portabilidade

- Caminhos legados de plano de contas apontam para `~/Downloads/` na máquina de desenvolvimento original. Em um novo ambiente, esses arquivos não existirão — mova os CSVs para os caminhos canônicos em `cfg/clientes/`.
- PDFs Zeus estão configurados com caminhos absolutos para o OneDrive local. Em novo ambiente, atualize `cfg/clientes/zeus/config.yml` com os caminhos corretos.
- O arquivo `.env` com as chaves Supabase não está no repositório — precisa ser recriado.
