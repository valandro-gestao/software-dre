"""
Geração do DRE (Demonstrativo de Resultado do Exercício) mensal.

Três papéis estruturais, mutuamente exclusivos (validados em
etl/plano_contas.py::_validar_contas_calculadas):

  1. Folha estrutural — NÃO é `conta_pai` de nenhuma outra linha, e não é
                     conta calculada. valor_base = Σ lançamentos cujo
                     categoria_origem (já mapeado) bate com a descricao.

  2. Agregadora estrutural — é `conta_pai` de outra(s) linha(s).
                     valor_base = Σ filhos.valor_base (recursivo, bottom-up,
                     por quantos níveis a árvore tiver — sem limite fixo).

  3. Conta calculada (tipo_conta=calculada) — valor_base vem EXCLUSIVAMENTE
                     da fórmula declarada em `formula`, nunca de rollup de
                     filhos nem de lançamentos mapeados (não pode ter filhos
                     nem categoria_origem — validado na carga do plano).
                     valor_base = Σ coeficiente_i × valor_base(id_conta_i),
                     resolvido recursivamente — uma conta calculada pode
                     referenciar folha, agregadora ou outra conta calculada,
                     em qualquer ordem física no CSV (a ordem de dependência
                     é resolvida por recursão memoizada, com detecção de
                     ciclo, não pela ordem das linhas).

Em todos os três casos, o campo `sinal` do plano é aplicado UMA ÚNICA VEZ,
por igual, sobre o valor_base já consolidado — nunca durante o rollup/soma
de termos. O valor_base sempre usa o sinal natural das transações (receitas
positivas, despesas negativas); `sinal` só entra no passo final de exibição.

Uma conta com múltiplas linhas físicas no CSV (mesmo `id_conta`, várias
`categoria_origem` apontando para a mesma conta) é tratada como um único
nó — o rollup do pai soma cada `id_conta` uma vez só, não uma vez por
linha física.

Fallback legado (_FORMULAS_RESULTADO)
--------------------------------------
Nenhum plano hoje declara `tipo_conta=calculada`. Para não quebrar Zixbe,
Zeus legado e Zeus novo, as 4 linhas de resultado abaixo continuam sendo
calculadas pelo mecanismo ANTIGO quando (e só quando) uma linha com aquele
nome exato NÃO tiver sido explicitamente declarada como calculada no plano:

  RECEITA LÍQUIDA, MARGEM DE CONTRIBUIÇÃO, RESULTADO OPERACIONAL, RESULTADO

Esse fallback opera sobre os valores JÁ EXIBIDOS (pós-sinal) dos componentes
— semântica diferente da conta calculada nova (que opera em valor_base
natural, sinal aplicado depois) — preservada assim deliberadamente, pixel a
pixel, para não alterar nenhum resultado numérico de plano existente. As
duas semânticas coexistem sem se misturar: o fallback só é consultado para
uma descricao se NENHUMA linha com esse nome tiver sido declarada calculada.
É um mecanismo de transição — deve ser removido quando todos os planos
declararem suas próprias fórmulas (fora do escopo desta rodada).
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

from etl.plano_contas import parse_formula

logger = logging.getLogger(__name__)

# Fórmulas das linhas de resultado, em ordem de dependência — FALLBACK LEGADO,
# usado só para descricao que não tenha sido explicitamente declarada
# tipo_conta=calculada em nenhuma linha do plano (ver docstring do módulo).
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


def _construir_arvore(plano_df: pd.DataFrame):
    """
    A partir do plano (podendo ter linhas duplicadas por id_conta),
    devolve:
        ids_unicos       — array de id_conta únicos
        filhos_de        — {id_conta: set(id_conta dos filhos)}
        descricao_por_id — {id_conta: descricao}
        sinal_por_id     — {id_conta: sinal}
        formula_por_id   — {id_conta: [(coeficiente, id_conta_ref), ...]}
                            só para linhas com tipo_conta=calculada.
    """
    ids_unicos = plano_df["id_conta"].dropna().astype(int).unique()

    filhos_de: Dict[int, set] = {}
    for _, row in plano_df.iterrows():
        pai = row["conta_pai"]
        if pd.notna(pai):
            filhos_de.setdefault(int(pai), set()).add(int(row["id_conta"]))

    descricao_por_id: Dict[int, str] = {}
    sinal_por_id: Dict[int, float] = {}
    formula_por_id: Dict[int, list] = {}
    for idc in ids_unicos:
        # linhas duplicadas do mesmo id_conta compartilham descricao/sinal —
        # a primeira ocorrência basta.
        linha = plano_df[plano_df["id_conta"] == idc].iloc[0]
        descricao_por_id[idc] = linha["descricao"]
        sinal_por_id[idc] = linha["sinal"]
        if str(linha.get("tipo_conta") or "").strip().lower() == "calculada":
            # Já validado (sintaxe, referências, ciclos) em
            # etl/plano_contas.py::_validar_contas_calculadas quando o plano
            # foi carregado — aqui só reaproveitamos o parser.
            formula_por_id[idc] = parse_formula(linha.get("formula"), idc)

    return ids_unicos, filhos_de, descricao_por_id, sinal_por_id, formula_por_id


def gerar_dre(lancamentos: List[dict], plano_df: pd.DataFrame) -> List[dict]:
    """
    Gera as linhas do DRE mensal.

    Args:
        lancamentos: lista de dicts de ResultadoETL.lancamentos (valor ainda float).
        plano_df:    DataFrame retornado por ler_plano_contas_df().

    Returns:
        Lista de dicts {mes, ordem, nivel, descricao, valor} ordenada por (mes, ordem).
        Inclui todas as contas do plano (uma linha por linha física do CSV,
        mesmo que o id_conta se repita), mesmo as de valor zero.
    """
    if plano_df.empty or not lancamentos:
        return []

    df_lanc = pd.DataFrame(lancamentos)
    df_lanc = df_lanc[df_lanc["categoria_origem"].notna()]

    meses = sorted(df_lanc["mes_referencia"].dropna().unique())
    resultado = []

    ids_unicos, filhos_de, descricao_por_id, sinal_por_id, formula_por_id = _construir_arvore(plano_df)
    ids_com_filhos = set(filhos_de.keys())
    ids_calculadas = set(formula_por_id.keys())
    # Folha estrutural = sem filhos e não calculada. Contas calculadas nunca
    # recebem lançamento direto, mesmo que estruturalmente childless.
    folhas_descs = {
        descricao_por_id[i] for i in ids_unicos
        if i not in ids_com_filhos and i not in ids_calculadas
    }

    for mes in meses:
        df_mes = df_lanc[df_lanc["mes_referencia"] == mes]

        # Soma bruta por categoria — mantém sinal natural das transações
        soma_cat: dict = df_mes.groupby("categoria_origem")["valor"].sum().to_dict()

        # Diagnóstico: quais categorias dos lançamentos bateram com uma conta-folha
        cats_com_match = {c for c in soma_cat if c in folhas_descs}
        cats_sem_match = {c for c in soma_cat if c not in folhas_descs}
        logger.info(
            "DRE [%s]: %d categoria(s) com match em conta-folha | %d sem match (valor perdido)",
            mes, len(cats_com_match), len(cats_sem_match),
        )
        if cats_sem_match:
            for cat in sorted(cats_sem_match):
                logger.warning(
                    "DRE [%s]: categoria_origem %r não encontrada em nenhuma conta-folha "
                    "do plano → valor=%.2f ignorado no DRE.",
                    mes, cat, soma_cat[cat],
                )

        # Resolução unificada, recursiva e memoizada, de valor_base — bottom-up,
        # sem limite de profundidade e sem depender da ordem física das linhas:
        #   - conta calculada:      Σ coeficiente_i × valor_base(id_conta_i)
        #   - agregadora estrutural: Σ filhos.valor_base (deduplicado por id_conta)
        #   - folha estrutural:      soma dos lançamentos mapeados a ela
        # `computing` detecta dependência circular entre contas calculadas
        # (a única forma de ciclo possível — já pré-validada na carga do
        # plano, mas mantida aqui como segunda linha de defesa).
        valor_base_cache: Dict[int, float] = {}
        computing: set = set()

        def _calc_valor_base(id_conta: int) -> float:
            if id_conta in valor_base_cache:
                return valor_base_cache[id_conta]
            if id_conta in computing:
                raise ValueError(
                    f"Dependência circular detectada envolvendo conta id={id_conta}."
                )
            computing.add(id_conta)
            if id_conta in ids_calculadas:
                valor = 0.0
                for coef, ref_id in formula_por_id[id_conta]:
                    if ref_id not in descricao_por_id:
                        raise ValueError(
                            f"Fórmula da conta id={id_conta} referencia id_conta={ref_id}, "
                            "que não está no plano fornecido a gerar_dre() — pode ter sido "
                            "excluído por exibir_dre=False antes de chegar aqui."
                        )
                    valor += coef * _calc_valor_base(ref_id)
            else:
                filhos = filhos_de.get(id_conta)
                if filhos:
                    valor = sum(_calc_valor_base(f) for f in filhos)
                else:
                    valor = soma_cat.get(descricao_por_id[id_conta], 0.0)
            computing.discard(id_conta)
            valor_base_cache[id_conta] = valor
            return valor

        for idc in ids_unicos:
            _calc_valor_base(int(idc))

        # Aplica sinal para exibição — uma vez, sobre o valor_base já consolidado,
        # por igual para folha, agregadora e conta calculada.
        valor_final_por_id: Dict[int, float] = {
            idc: round(valor_base_cache[idc] * (sinal_por_id[idc] or 0), 2)
            for idc in ids_unicos
        }

        # Fallback LEGADO — só para descricao de _FORMULAS_RESULTADO cujas
        # linhas NÃO tenham sido explicitamente declaradas tipo_conta=calculada
        # em nenhuma linha do plano. Opera sobre valores JÁ EXIBIDOS (pós-sinal),
        # exatamente como antes desta mudança — preservado pixel a pixel para
        # não alterar nenhum resultado de plano existente (ver docstring do módulo).
        valor_por_desc: Dict[str, float] = {
            descricao_por_id[i]: valor_final_por_id[i] for i in ids_unicos
        }
        for descricao, componentes in _FORMULAS_RESULTADO.items():
            ids_com_essa_descricao = [i for i in ids_unicos if descricao_por_id[i] == descricao]
            ids_legado = [i for i in ids_com_essa_descricao if i not in ids_calculadas]
            if not ids_legado:
                # Todas as linhas com esse nome já foram resolvidas via fórmula declarada.
                continue
            total = sum(op * valor_por_desc.get(dep, 0.0) for op, dep in componentes)
            total = round(total, 2)
            for i in ids_legado:
                valor_final_por_id[i] = total
            valor_por_desc[descricao] = total

        # Gera linhas ordenadas por ordem — uma linha por linha física do plano.
        for _, row in plano_df.sort_values("ordem").iterrows():
            idc = int(row["id_conta"])
            resultado.append({
                "mes":      mes,
                "ordem":    int(row["ordem"]),
                "nivel":    int(row["nivel"]),
                "descricao": row["descricao"],
                "valor":    round(float(valor_final_por_id[idc]), 2),
            })

    return resultado
