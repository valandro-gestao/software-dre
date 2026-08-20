"""
Configurações por cliente para o ETL do Conta Azul.

Estrutura de caminhos (canônica):
  cfg/clientes/{cliente}/config.yml              ← config de PDFs (Zeus) etc.
  cfg/clientes/{cliente}/plano_contas_cliente.csv

  data/raw/{cliente}/{mes_ref}/                  ← arquivos originais do cliente
  saida/{cliente}/{mes_ref}/                     ← CSVs gerados

Para adicionar um novo cliente:
  1. Crie cfg/clientes/{nome_lower}/config.yml   (se usar PDFs)
  2. Copie o plano de contas para cfg/clientes/{nome_lower}/plano_contas_cliente.csv
  3. Adicione a entrada em CLIENTES abaixo.
  4. Implemente etl/clients/{nome_lower}.py se necessário.
"""

TOLERANCIA_CENTAVOS = 0.01  # R$0,01 de tolerância para arredondamento

CLIENTES: dict = {
    # ── Zeus ──────────────────────────────────────────────────────────────────
    # Fonte: PDFs Wingraph.
    # Configuração de PDFs por mês: cfg/clientes/zeus/config.yml
    # Plano de contas:              cfg/clientes/zeus/plano_contas_cliente.csv
    # Arquivos brutos:              data/raw/zeus/{mes_ref}/
    # Uso: python main.py --client zeus --mes-ref 2026-01
    "zeus": {
        "nome": "Zeus",
        "tipo_fonte": "pdf",

        # Caminho canônico — resolver buscará aqui primeiro.
        # Legado (fallback): "cfg/zeus.yml"
        "config_yaml_path": "cfg/clientes/zeus/config.yml",

        # Caminho canônico — resolver buscará aqui primeiro.
        # Legado (fallback): "/Users/rafaelpaim/Downloads/plano_contas_zeus.csv"
        "plano_contas_path": "cfg/clientes/zeus/plano_contas_cliente.csv",
        "plano_contas_legado": "/Users/rafaelpaim/Downloads/plano_contas_zeus.csv",

        "mapeamento_categorias": {},
    },

    # ── Zixbe ─────────────────────────────────────────────────────────────────
    # Fonte: Excel Conta Azul (visão de competência).
    # Plano de contas: cfg/clientes/zixbe/plano_contas_cliente.csv
    # Arquivos brutos: data/raw/zixbe/{mes_ref}/
    # Uso: python main.py data/raw/zixbe/2026-04/arquivo.xlsx --client zixbe --mes-ref 2026-04
    "zixbe": {
        "nome": "Zixbe",

        # Colunas fixas do relatório Conta Azul (visão de competência)
        "colunas_fixas": {
            "data_competencia": "Data de competência",
            "descricao": "Descrição",
            "fornecedor_cliente": "Nome do fornecedor/cliente",
            "valor_total": "Valor (R$)",
        },
        # Prefixos das colunas dinâmicas (N = 1, 2, 3 …)
        "prefixos": {
            "categoria": "Categoria",
            "valor_categoria": "Valor na Categoria",
            "centro_custo": "Centro de Custo",
            "valor_centro_custo": "Valor no Centro de Custo",
        },

        # Caminho canônico — resolver buscará aqui primeiro.
        # Legado (fallback): "/Users/rafaelpaim/Downloads/plano_contas_zixbe.csv"
        "plano_contas_path": "cfg/clientes/zixbe/plano_contas_cliente.csv",
        "plano_contas_legado": "/Users/rafaelpaim/Downloads/plano_contas_zixbe.csv",

        # Sobrescritas manuais aplicadas APÓS o plano de contas.
        "mapeamento_categorias": {
            # "Nome Original no Conta Azul": "Nome Padronizado",
        },
    },
}
