"""
Geração do DRE (Demonstrativo de Resultado do Exercício) mensal.

Algoritmo
---------
Para cada mês presente nos lançamentos:

  1. Nivel 3  — soma dos valores das transações por categoria_origem.
               valor_base = Σ lançamentos[categoria_origem == descricao]

  2. Nivel 2  — rollup dos filhos de nivel 3.
               valor_base = Σ filhos.valor_base

  3. Nivel 1  — rollup dos filhos de nivel 2.
               valor_base = Σ filhos.valor_base

  4. Display  — aplica o campo sinal do plano de contas:
               valor = valor_base × sinal

O valor_base sempre usa o sinal natural das transações do Conta Azul
(receitas positivas, despesas negativas). O campo `sinal` só é aplicado
na última etapa, garantindo que receitas e despesas apareçam com sinal
correto no DRE (convenção: ambos positivos, com a estrutura do plano
indicando se são deduções ou custos).

Linhas de resultado (passo 5)
------------------------------
Linhas sintéticas sem filhos diretos no plano (RECEITA LÍQUIDA, MARGEM DE
CONTRIBUIÇÃO, RESULTADO OPERACIONAL, RESULTADO) são calculadas por fórmula
sobre os valores já exibidos dos grupos principais, em ordem de dependência.
"""

import logging
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)

# Fórmulas das linhas de resultado, em ordem de dependência.
# Cada entrada: descricao_resultado → [(sinal, descricao_componente), ...]
# O `sinal` aqui é o operador aritmético (+1 / -1), não o campo do plano.
# Todos os componentes já tiveram o campo `sinal` do plano aplicado.
_FORMULAS_RESULTADO = {
    "RECEITA LÍQUIDA": [
        (+1, "RECEITA BRUTA"),
        (-1, "DEDUÇÕES DA RECEITA"),
    ],
    "MARGEM DE CONTRIBUIÇÃO": [
        (+1, "RECEITA LÍQUIDA"),
        (-1, "CUSTO VARIÁVEL"),
    ],
    "RESULTADO OPERACIONAL": [
        (+1, "MARGEM DE CONTRIBUIÇÃO"),
        (-1, "DESPESAS FIXAS"),
    ],
    "RESULTADO": [
        (+1, "RESULTADO OPERACIONAL"),
        (+1, "RECEITAS FINANCEIRAS"),
        (-1, "DESPESAS FINANCEIRAS"),
        (-1, "PASSIVO"),
        (-1, "INVESTIMENTOS"),
        (-1, "DIVISÃO DE LUCROS"),
    ],
}


def gerar_dre(lancamentos: List[dict], plano_df: pd.DataFrame) -> List[dict]:
    """
    Gera as linhas do DRE mensal.

    Args:
        lancamentos: lista de dicts de ResultadoETL.lancamentos (valor ainda float).
        plano_df:    DataFrame retornado por ler_plano_contas_df().

    Returns:
        Lista de dicts {mes, ordem, nivel, descricao, valor} ordenada por (mes, ordem).
        Inclui todas as contas do plano, mesmo as de valor zero.
    """
    if plano_df.empty or not lancamentos:
        return []

    df_lanc = pd.DataFrame(lancamentos)
    df_lanc = df_lanc[df_lanc["categoria_origem"].notna()]

    meses = sorted(df_lanc["mes_referencia"].dropna().unique())
    resultado = []

    for mes in meses:
        df_mes = df_lanc[df_lanc["mes_referencia"] == mes]

        # Soma bruta por categoria (nivel 3) — mantém sinal natural das transações
        soma_cat: dict = (
            df_mes.groupby("categoria_origem")["valor"].sum().to_dict()
        )

        # Cópia do plano para este mês com coluna de valor acumulado
        plano = plano_df.copy()
        plano["valor_base"] = 0.0

        # 1. Atribui valor_base para nivel 3
        mask3 = plano["nivel"] == 3
        plano.loc[mask3, "valor_base"] = (
            plano.loc[mask3, "descricao"].map(soma_cat).fillna(0.0)
        )

        # Diagnóstico: quais categorias dos lançamentos bateram com nivel-3
        nivel3_descs = set(plano.loc[mask3, "descricao"])
        cats_com_match    = {c for c in soma_cat if c in nivel3_descs}
        cats_sem_match    = {c for c in soma_cat if c not in nivel3_descs}
        logger.info(
            "DRE [%s]: %d categoria(s) com match nivel-3 | %d sem match (valor perdido)",
            mes, len(cats_com_match), len(cats_sem_match),
        )
        if cats_sem_match:
            for cat in sorted(cats_sem_match):
                logger.warning(
                    "DRE [%s]: categoria_origem %r não encontrada em nivel-3 "
                    "→ valor=%.2f ignorado no DRE.",
                    mes, cat, soma_cat[cat],
                )

        # 2. Rollup nivel 2 ← soma dos filhos nivel 3
        for _, n2 in plano[plano["nivel"] == 2].iterrows():
            filhos = plano[(plano["conta_pai"] == n2["id_conta"]) & mask3]
            plano.loc[plano["id_conta"] == n2["id_conta"], "valor_base"] = (
                filhos["valor_base"].sum()
            )

        # 3. Rollup nivel 1 ← soma dos filhos nivel 2
        mask2 = plano["nivel"] == 2
        for _, n1 in plano[plano["nivel"] == 1].iterrows():
            filhos = plano[(plano["conta_pai"] == n1["id_conta"]) & mask2]
            plano.loc[plano["id_conta"] == n1["id_conta"], "valor_base"] = (
                filhos["valor_base"].sum()
            )

        # 4. Aplica sinal para exibição
        plano["valor"] = plano["valor_base"] * plano["sinal"].fillna(0)

        # 5. Calcula linhas de resultado por fórmula (em ordem de dependência)
        # valor_por_desc é atualizado a cada cálculo para que fórmulas
        # subsequentes possam depender de resultados já calculados.
        valor_por_desc: dict = dict(zip(plano["descricao"], plano["valor"]))
        for descricao, componentes in _FORMULAS_RESULTADO.items():
            total = sum(op * valor_por_desc.get(dep, 0.0) for op, dep in componentes)
            total = round(total, 2)
            plano.loc[plano["descricao"] == descricao, "valor"] = total
            valor_por_desc[descricao] = total

        # Gera linhas ordenadas por ordem
        for _, row in plano.sort_values("ordem").iterrows():
            resultado.append({
                "mes":      mes,
                "ordem":    int(row["ordem"]),
                "nivel":    int(row["nivel"]),
                "descricao": row["descricao"],
                "valor":    round(float(row["valor"]), 2),
            })

    return resultado
