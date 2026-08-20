"""
Gera um arquivo Excel de exemplo no formato Conta Azul (visão de competência)
para testar o ETL sem precisar de dados reais.

    python gerar_exemplo.py
"""

from pathlib import Path
import pandas as pd


COLUNAS = [
    "Data Competência",
    "Histórico",
    "Fornecedor / Cliente",
    "Categoria 1",
    "Valor na Categoria 1",
    "Categoria 2",
    "Valor na Categoria 2",
    "Centro de Custo 1",
    "Valor no Centro de Custo 1",
    "Centro de Custo 2",
    "Valor no Centro de Custo 2",
    "Valor (R$)",
]

DADOS = [
    # Caso 1: linha simples (uma categoria, sem CC)
    ["2024-03-05", "Pagamento de fornecedor", "Empresa Alpha",
     "Serviços Terceiros", -1500.00, None, None, None, None, None, None, -1500.00],

    # Caso 2: múltiplas categorias, sem CC
    ["2024-03-10", "Nota fiscal composta", "Empresa Beta",
     "Software", -800.00, "Infraestrutura", -200.00, None, None, None, None, -1000.00],

    # Caso 3: múltiplos centros de custo (prioridade sobre categorias)
    ["2024-03-15", "Rateio de despesa", "Empresa Gamma",
     "Despesas Gerais", -600.00, None, None, "TI", -400.00, "Marketing", -200.00, -600.00],

    # Caso 4: receita positiva, linha simples
    ["2024-03-20", "Recebimento de cliente", "Cliente Delta",
     "Receita de Serviços", 5000.00, None, None, None, None, None, None, 5000.00],

    # Caso 5: divergência intencional para teste de inconsistência
    ["2024-03-25", "Lançamento com erro", "Fornecedor Epsilon",
     "Marketing", -300.00, "Vendas", -100.00, None, None, None, None, -500.00],

    # Caso 6: apenas centro de custo 1
    ["2024-03-28", "Despesa única CC", "Fornecedor Zeta",
     "Pessoal", -2000.00, None, None, "RH", -2000.00, None, None, -2000.00],
]

df = pd.DataFrame(DADOS, columns=COLUNAS)

# Converte data
df["Data Competência"] = pd.to_datetime(df["Data Competência"])

output = Path("sample_data/exemplo_conta_azul.xlsx")
output.parent.mkdir(exist_ok=True)
df.to_excel(output, index=False)
print(f"Arquivo gerado: {output}")
print(df.to_string(index=False))
