"""
Leitura e validação do plano de contas CSV.

Formato esperado (separador ; ou ,):
  id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem;tipo_conta;formula

Colunas obrigatórias:
  id_conta, nivel, conta_pai, ordem, descricao, sinal

Colunas opcionais (retrocompatíveis — não precisam existir no arquivo):
  exibir_dre      (bool, default True)  — inclui a linha no DRE
  auditoria_only  (bool, default False) — categoria de conferência, não contribui ao DRE
  categoria_origem (str)                — como a categoria vem do ERP/sistema
  tipo_conta      (str, default "estrutural") — "calculada" marca uma conta cujo
                    valor vem exclusivamente da fórmula declarada em `formula`,
                    nunca de rollup de filhos nem de lançamentos mapeados.
  formula         (str)                — obrigatória quando tipo_conta=calculada.
                    Lista de termos separados por espaço, cada termo no formato
                    [+-]coeficiente*id_conta, ex.: "+1*1 -1*23". Referencia
                    SEMPRE por id_conta (nunca por descricao — descricao é
                    atributo de apresentação e pode ser renomeado sem quebrar
                    a fórmula). Ver parse_formula().

Papéis estruturais (mutuamente exclusivos, validados em _validar_plano):
  - folha estrutural      — sem filhos, tipo_conta != calculada; recebe lançamentos
                             mapeados via categoria_origem.
  - agregadora estrutural — tem filhos (é conta_pai de outra linha); soma os filhos.
  - conta calculada       — tipo_conta=calculada; valor vem só da fórmula declarada.
                             Não pode ter filhos nem categoria_origem preenchida.

Regras:
  - Múltiplas linhas podem ter a mesma descricao mas categoria_origem diferente,
    desde que compartilhem o mesmo id_conta.
  - exibir_dre=False ou auditoria_only=True: excluir do DRE, mas mapear a categoria
    (evita que apareça em categorias_nao_mapeadas).

Compatibilidade legada:
  Nenhum plano hoje declara tipo_conta/formula. As linhas de resultado
  (RECEITA LÍQUIDA, MARGEM DE CONTRIBUIÇÃO, RESULTADO OPERACIONAL, RESULTADO)
  continuam calculadas pelo fallback hardcoded em etl/dre.py::_FORMULAS_RESULTADO
  enquanto não forem declaradas explicitamente — ver _NOMES_LEGADO_CALCULADAS.
"""

import csv
import logging
import re
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_COLUNAS_OBRIGATORIAS = {"id_conta", "nivel", "conta_pai", "ordem", "descricao", "sinal"}

_BOOL_MAP = {
    "true": True, "1": True, "sim": True, "yes": True,
    "false": False, "0": False, "nao": False, "não": False, "no": False,
}

# Nomes reconhecidos pelo fallback legado (etl/dre.py::_FORMULAS_RESULTADO).
# Usado SÓ para não gerar aviso espúrio de "folha sem categoria_origem" nessas
# linhas em planos antigos (elas são, na prática, calculadas — só que ainda
# implicitamente, pelo nome, não por declaração explícita). Remover esta lista
# junto com _FORMULAS_RESULTADO quando o fallback legado for desativado.
_NOMES_LEGADO_CALCULADAS = {
    "RECEITA LÍQUIDA", "MARGEM DE CONTRIBUIÇÃO", "RESULTADO OPERACIONAL", "RESULTADO",
}

# Termo de fórmula: [+-]coeficiente*id_conta — ex.: "+1*1", "-2.5*23"
_RE_TERMO_FORMULA = re.compile(r'^([+-])(\d+(?:\.\d+)?)\*(\d+)$')


def parse_formula(formula_str: str, id_conta_contexto: Optional[int] = None) -> List[Tuple[float, int]]:
    """
    Faz o parse de uma fórmula declarada no plano.

    Sintaxe: termos separados por espaço, cada termo [+-]coeficiente*id_conta.
    Ex.: "+1*1 -1*23" → [(+1.0, 1), (-1.0, 23)]

    Não é um parser de expressão matemática genérico (sem precedência, sem
    parênteses, sem eval) — deliberadamente restrito a soma/subtração de
    termos coeficiente×conta, que é o que a fórmula de resultado de uma DRE
    precisa hoje.

    Levanta ValueError em qualquer sintaxe fora do formato.
    """
    if pd.isna(formula_str) or not str(formula_str).strip():
        raise ValueError(
            f"formula vazia ou ausente (conta id={id_conta_contexto})."
        )
    termos_raw = str(formula_str).split()
    resultado: List[Tuple[float, int]] = []
    for termo in termos_raw:
        m = _RE_TERMO_FORMULA.match(termo)
        if not m:
            raise ValueError(
                f"termo de fórmula inválido: {termo!r} (conta id={id_conta_contexto}) — "
                "formato esperado: [+-]coeficiente*id_conta, ex.: '+1*3' ou '-2.5*17'."
            )
        sinal_str, coef_str, id_str = m.groups()
        coef = float(coef_str) * (1.0 if sinal_str == "+" else -1.0)
        resultado.append((coef, int(id_str)))
    return resultado


def _detectar_delimitador(linha: str) -> str:
    """Detecta ',' ou ';' na linha de cabeçalho. Retorna ';' como fallback."""
    try:
        dialect = csv.Sniffer().sniff(linha, delimiters=",;")
        return dialect.delimiter
    except csv.Error:
        return ";"


def _limpar_linhas(raw_lines: list) -> list:
    """
    Remove aspas duplas externas e espaços em branco de cada linha.
    Linha: '"1;1;;10;RECEITA BRUTA;1"' → '1;1;;10;RECEITA BRUTA;1'
    """
    resultado = []
    for line in raw_lines:
        line = line.strip()
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        if line:
            resultado.append(line)
    return resultado


def _carregar_df(filepath: str) -> pd.DataFrame:
    """
    Lê o CSV, limpa aspas e detecta delimitador.
    Retorna DataFrame com colunas tipadas; colunas opcionais preenchidas com defaults.
    Retorna DataFrame vazio se arquivo não existir ou tiver colunas obrigatórias ausentes.
    """
    path = Path(filepath)
    if not path.exists():
        logger.warning("Plano de contas não encontrado: %s", filepath)
        return pd.DataFrame()

    with open(path, encoding="utf-8-sig") as f:
        raw_lines = f.readlines()

    cleaned = _limpar_linhas(raw_lines)
    if not cleaned:
        logger.warning("Plano de contas vazio: %s", filepath)
        return pd.DataFrame()

    sep = _detectar_delimitador(cleaned[0])
    logger.info("Plano de contas '%s': delimitador='%s'", path.name, sep)

    df = pd.read_csv(StringIO("\n".join(cleaned)), sep=sep, dtype=str)
    df.columns = [c.strip().strip('"') for c in df.columns]
    for col in df.select_dtypes("object").columns:
        df[col] = df[col].str.strip().str.strip('"')

    # Valida colunas obrigatórias
    ausentes = _COLUNAS_OBRIGATORIAS - set(df.columns)
    if ausentes:
        logger.error(
            "Plano de contas '%s': colunas obrigatórias ausentes: %s.\n"
            "  Formato esperado: id_conta;nivel;conta_pai;ordem;descricao;sinal"
            "[;exibir_dre;auditoria_only;categoria_origem]",
            path.name, ", ".join(sorted(ausentes)),
        )
        return pd.DataFrame()

    # Colunas numéricas
    df["id_conta"]  = pd.to_numeric(df["id_conta"],  errors="coerce").astype("Int64")
    df["nivel"]     = pd.to_numeric(df["nivel"],     errors="coerce").astype("Int64")
    df["ordem"]     = pd.to_numeric(df["ordem"],     errors="coerce").astype("Int64")
    df["sinal"]     = pd.to_numeric(df["sinal"],     errors="coerce").astype("Int64")
    df["conta_pai"] = pd.to_numeric(df["conta_pai"], errors="coerce").astype("Int64")

    # exibir_dre — default True quando coluna ausente ou valor inválido
    if "exibir_dre" not in df.columns:
        df["exibir_dre"] = True
    else:
        df["exibir_dre"] = (
            df["exibir_dre"].str.lower().map(_BOOL_MAP).fillna(True)
        )

    # auditoria_only — default False quando coluna ausente ou valor inválido
    if "auditoria_only" not in df.columns:
        df["auditoria_only"] = False
    else:
        df["auditoria_only"] = (
            df["auditoria_only"].str.lower().map(_BOOL_MAP).fillna(False)
        )

    # categoria_origem — coluna opcional
    if "categoria_origem" not in df.columns:
        df["categoria_origem"] = None
    else:
        # String vazia → None
        df["categoria_origem"] = df["categoria_origem"].replace("", None)

    # tipo_conta — coluna opcional. Default "estrutural" quando ausente ou
    # valor vazio/desconhecido. Único valor especial reconhecido: "calculada".
    if "tipo_conta" not in df.columns:
        df["tipo_conta"] = "estrutural"
    else:
        _tipo_norm = df["tipo_conta"].str.strip().str.lower()
        df["tipo_conta"] = _tipo_norm.where(_tipo_norm == "calculada", "estrutural")

    # formula — coluna opcional, só tem significado quando tipo_conta=calculada.
    if "formula" not in df.columns:
        df["formula"] = None
    else:
        df["formula"] = df["formula"].replace("", None)

    return df


def _validar_contas_calculadas(df: pd.DataFrame, filepath: str) -> Dict[int, List[Tuple[float, int]]]:
    """
    Valida toda conta com tipo_conta=calculada e devolve {id_conta: termos_da_formula}.

    Levanta ValueError (erro explícito, bloqueante — nunca falha silenciosa)
    para:
      - tipo_conta=calculada com formula vazia/ausente;
      - sintaxe de formula inválida (ver parse_formula);
      - formula referenciando id_conta que não existe no plano;
      - autorreferência direta (formula da conta X referencia X);
      - dependência circular entre contas calculadas (X → Y → X);
      - conta calculada que também é agregadora (tem filhos);
      - conta calculada com categoria_origem preenchida.

    Não faz nada (retorna {}) se nenhuma linha declarar tipo_conta=calculada —
    zero risco para planos legados, que nunca têm essa coluna.
    """
    nome_arquivo = Path(filepath).name
    calculadas = df[df["tipo_conta"] == "calculada"]
    if calculadas.empty:
        return {}

    ids_validos = set(int(x) for x in df["id_conta"].dropna().unique())
    ids_pai = ids_agregadores(df)

    formula_por_id: Dict[int, List[Tuple[float, int]]] = {}
    for _, row in calculadas.iterrows():
        idc = int(row["id_conta"])

        if idc in ids_pai:
            raise ValueError(
                f"Plano '{nome_arquivo}': conta id={idc} ('{row['descricao']}') é "
                "tipo_conta=calculada mas também é agregadora (é conta_pai de outra "
                "linha) — uma conta calculada não pode ter filhos."
            )

        cat_orig = row.get("categoria_origem")
        if pd.notna(cat_orig) and str(cat_orig).strip() != "":
            raise ValueError(
                f"Plano '{nome_arquivo}': conta id={idc} ('{row['descricao']}') é "
                f"tipo_conta=calculada mas tem categoria_origem={cat_orig!r} preenchida "
                "— uma conta calculada não recebe lançamentos mapeados."
            )

        termos = parse_formula(row.get("formula"), idc)  # levanta ValueError se vazia/inválida

        for _coef, ref_id in termos:
            if ref_id == idc:
                raise ValueError(
                    f"Plano '{nome_arquivo}': fórmula da conta id={idc} "
                    f"('{row['descricao']}') referencia a si mesma (autorreferência)."
                )
            if ref_id not in ids_validos:
                raise ValueError(
                    f"Plano '{nome_arquivo}': fórmula da conta id={idc} "
                    f"('{row['descricao']}') referencia id_conta={ref_id}, que não "
                    "existe no plano."
                )

        formula_por_id[idc] = termos

    # Dependência circular entre contas calculadas (grafo calculada → calculada).
    ids_calculadas = set(formula_por_id.keys())
    grafo = {
        idc: [ref for _, ref in termos if ref in ids_calculadas]
        for idc, termos in formula_por_id.items()
    }
    visitando: set = set()
    visitado: set = set()

    def _dfs(idc: int, caminho: List[int]) -> None:
        if idc in visitando:
            ciclo = " → ".join(str(x) for x in caminho + [idc])
            raise ValueError(
                f"Plano '{nome_arquivo}': dependência circular entre contas "
                f"calculadas: {ciclo}."
            )
        if idc in visitado:
            return
        visitando.add(idc)
        for vizinho in grafo.get(idc, []):
            _dfs(vizinho, caminho + [idc])
        visitando.discard(idc)
        visitado.add(idc)

    for idc in ids_calculadas:
        _dfs(idc, [])

    return formula_por_id


def _validar_plano(df: pd.DataFrame, filepath: str) -> None:
    """
    Loga avisos sobre problemas de preenchimento no plano e levanta ValueError
    para estruturas de conta calculada inválidas (ver _validar_contas_calculadas).
    """
    # Folha estrutural sem categoria_origem: aviso, não erro — a conta só não
    # será mapeada automaticamente via CSV. Não depende mais de `nivel`: uma
    # folha pode estar em qualquer nível. Contas calculadas (explícitas via
    # tipo_conta, ou implícitas via o fallback legado _NOMES_LEGADO_CALCULADAS)
    # são excluídas — elas nunca têm categoria_origem por design, não por lacuna.
    ids_pai = ids_agregadores(df)
    e_calculada = df["tipo_conta"] == "calculada"
    e_legado_calculada = df["descricao"].isin(_NOMES_LEGADO_CALCULADAS)
    e_folha_estrutural = (~df["id_conta"].isin(ids_pai)) & (~e_calculada) & (~e_legado_calculada)

    sem_cat = df[
        e_folha_estrutural
        & (df["categoria_origem"].isna() | (df["categoria_origem"] == ""))
    ]
    if not sem_cat.empty:
        exemplos = ", ".join(
            f'"{d}"' for d in sem_cat["descricao"].dropna().tolist()[:5]
        )
        logger.warning(
            "Plano '%s': %d conta(s)-folha sem categoria_origem "
            "(não serão mapeadas automaticamente via CSV): %s%s",
            Path(filepath).name,
            len(sem_cat),
            exemplos,
            " …" if len(sem_cat) > 5 else "",
        )

    _validar_contas_calculadas(df, filepath)

    # Estrutura inválida: conta com categoria_origem precisa ser folha
    # (sem filhos). Conta agregadora (é conta_pai de outra linha) que
    # também tenha categoria_origem preenchida é ambígua — não é tratada
    # como "valor próprio + filhos"; a categoria_origem é ignorada na
    # construção do mapeamento (ver ler_mapeamento_plano).
    ids_pai = ids_agregadores(df)
    agregadores_com_cat = df[
        df["id_conta"].isin(ids_pai)
        & df["categoria_origem"].notna()
        & (df["categoria_origem"] != "")
    ]
    if not agregadores_com_cat.empty:
        for _, row in agregadores_com_cat.iterrows():
            logger.error(
                "Plano '%s': conta agregadora '%s' (id=%s) tem categoria_origem=%r "
                "preenchida — estrutura inválida. Conta com categoria_origem precisa "
                "ser folha (sem filhos); conta com filhos é agregadora. "
                "Essa categoria_origem será ignorada no mapeamento.",
                Path(filepath).name, row["descricao"], row["id_conta"],
                row["categoria_origem"],
            )


def ler_plano_contas_df(filepath: str) -> pd.DataFrame:
    """Retorna o plano de contas completo como DataFrame tipado."""
    df = _carregar_df(filepath)
    if not df.empty:
        logger.info("Plano de contas: %d conta(s) carregada(s).", len(df))
        _validar_plano(df, filepath)
    return df


def ids_agregadores(df: pd.DataFrame) -> set:
    """IDs que são conta_pai de alguma linha — ou seja, contas agregadoras
    (têm filhos). Independe do valor de `nivel`."""
    return set(
        int(v) for v in df["conta_pai"].dropna().tolist()
    )


def ler_mapeamento_plano(filepath: Optional[str]) -> Dict[str, str]:
    """
    Constrói o mapeamento {categoria_origem → descricao} a partir do CSV.

    Fonte: contas-folha (sem filhos — não dependem de estar em nível 3)
    com categoria_origem preenchida. Múltiplas linhas podem ter
    categoria_origem diferente mas apontar para a mesma descricao (conta
    gerencial) ou para o mesmo id_conta.

    Regra de estrutura: conta com categoria_origem precisa ser folha
    (sem filhos); conta agregadora (com filhos) que também tenha
    categoria_origem preenchida é estrutura inválida — essa
    categoria_origem é ignorada aqui (o erro é registrado por
    _validar_plano(), chamada por ler_plano_contas_df()).

    Inclui linhas auditoria_only=True no mapeamento para que essas categorias
    não apareçam em categorias_nao_mapeadas, mas elas ficam fora do DRE
    (filtradas por exibir_dre=False em pipeline.py).

    Retorna {} se filepath for None ou arquivo não existir.
    """
    if not filepath:
        return {}

    df = _carregar_df(filepath)
    if df.empty:
        return {}

    ids_pai = ids_agregadores(df)
    com_cat = df[df["categoria_origem"].notna() & (df["categoria_origem"] != "")]
    folhas_com_cat = com_cat[~com_cat["id_conta"].isin(ids_pai)]

    ignoradas = com_cat[com_cat["id_conta"].isin(ids_pai)]
    if not ignoradas.empty:
        logger.debug(
            "Plano de contas: %d linha(s) agregadora(s) com categoria_origem "
            "preenchida foram ignoradas na construção do mapeamento (estrutura "
            "inválida — ver _validar_plano).",
            len(ignoradas),
        )

    mapeamento: Dict[str, str] = {}
    n_dre = 0
    n_auditoria = 0

    for _, row in folhas_com_cat.iterrows():
        cat_orig  = str(row["categoria_origem"]).strip()
        descricao = str(row["descricao"]).strip()
        if not cat_orig or not descricao:
            continue
        mapeamento[cat_orig] = descricao
        if row.get("auditoria_only", False):
            n_auditoria += 1
        else:
            n_dre += 1

    logger.info(
        "Plano de contas: %d categoria(s) mapeada(s) via categoria_origem "
        "(%d DRE + %d auditoria_only).",
        len(mapeamento), n_dre, n_auditoria,
    )
    return mapeamento


def ler_plano_contas(filepath: str) -> Dict[str, str]:
    """
    Legado: retorna {descricao_nivel3: descricao_nivel3}.
    Prefira ler_mapeamento_plano() que usa a coluna categoria_origem do CSV.
    """
    df = _carregar_df(filepath)
    if df.empty:
        return {}
    nivel3 = df[df["nivel"] == 3]["descricao"].dropna().unique()
    mapeamento = {desc: desc for desc in nivel3 if desc}
    logger.info("Plano de contas (legado): %d categoria(s) nível 3 carregada(s).", len(mapeamento))
    return mapeamento
