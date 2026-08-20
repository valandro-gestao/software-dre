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
| `dre_mensal.csv` | DRE estruturado por nível (1, 2, 3) e linhas de resultado |
| `resumo_conferencia.csv` | Total ERP × Total ETL, diferença e contagens |
| `resumo_auditoria.csv` | Por empresa/tipo (apenas Zeus multi-CNPJ) |
| `inconsistencias.csv` | Lançamentos com problemas de parsing |
| `categorias_nao_mapeadas.csv` | Categorias sem correspondência no plano de contas |

---

## Plano de contas

### Formato atualmente suportado pelo código

Separador `;` ou `,` (detectado automaticamente). Colunas:

```
id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem
```

| Coluna | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `id_conta` | int | Sim | Identificador único da conta |
| `nivel` | int (1–3) | Sim | Nível hierárquico (1=grupo, 2=subgrupo, 3=conta) |
| `conta_pai` | int | Sim | `id_conta` do nível acima (vazio no nível 1) |
| `ordem` | int | Sim | Ordem de exibição no DRE |
| `descricao` | str | Sim | Nome gerencial da conta |
| `sinal` | int (+1/-1) | Sim | Inverte o sinal para exibição (+receitas / −despesas) |
| `exibir_dre` | bool | Não¹ | Inclui a linha no DRE (padrão: `true`) |
| `auditoria_only` | bool | Não¹ | Categoria de conferência, mapeada mas fora do DRE (padrão: `false`) |
| `categoria_origem` | str | Não¹ | Nome exato da categoria no ERP; chave do mapeamento automático |

¹ Colunas opcionais com retrocompatibilidade: CSVs no formato antigo (6 colunas) funcionam com os valores padrão.

### Regras

- Níveis 1 e 2 podem ter `categoria_origem` vazio.
- Nível 3 deve ter `categoria_origem` preenchido (o pipeline emite aviso se ausente).
- Múltiplas linhas de nível 3 podem ter `categoria_origem` diferentes apontando para a mesma `descricao`.
- `auditoria_only=true`: categoria mapeada para evitar falso positivo em `categorias_nao_mapeadas.csv`, mas excluída do DRE.
- Booleanos aceitos: `Sim/Não`, `true/false`, `1/0`, `sim/não` (case-insensitive).

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

1. **Nível 3** — soma dos lançamentos cujo `categoria_origem` bate com `descricao` de uma conta nível 3 com `exibir_dre=True`.
2. **Nível 2** — rollup: soma dos filhos de nível 3.
3. **Nível 1** — rollup: soma dos filhos de nível 2.
4. **Sinal** — cada linha é multiplicada pelo campo `sinal` do plano.
5. **Linhas de resultado** — calculadas por fórmulas em `etl/dre.py`:
   - `RECEITA LÍQUIDA = RECEITA BRUTA − DEDUÇÕES DA RECEITA`
   - `MARGEM DE CONTRIBUIÇÃO = RECEITA LÍQUIDA − CUSTO VARIÁVEL`
   - `RESULTADO OPERACIONAL = MARGEM DE CONTRIBUIÇÃO − DESPESAS FIXAS`
   - `RESULTADO = RESULTADO OPERACIONAL + RECEITAS FINANCEIRAS − DESPESAS FINANCEIRAS − PASSIVO − INVESTIMENTOS − DIVISÃO DE LUCROS`

Os nomes das linhas de resultado devem existir exatamente como `descricao` no plano de contas.

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
- O plano atual usa o formato novo (9 colunas).

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
