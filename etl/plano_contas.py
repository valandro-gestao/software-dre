"""
Leitura e validação do plano de contas CSV.

Formato esperado (separador ; ou ,):
  id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem

Colunas obrigatórias:
  id_conta, nivel, conta_pai, ordem, descricao, sinal

Colunas opcionais (retrocompatíveis — não precisam existir no arquivo):
  exibir_dre      (bool, default True)  — inclui a linha no DRE
  auditoria_only  (bool, default False) — categoria de conferência, não contribui ao DRE
  categoria_origem (str)                — como a categoria vem do ERP/sistema

Regras:
  - Níveis 1 e 2 podem ter categoria_origem vazio.
  - Nível 3 deve ter categoria_origem preenchido (aviso se ausente).
  - Múltiplas linhas podem ter a mesma descricao mas categoria_origem diferente.
  - exibir_dre=False ou auditoria_only=True: excluir do DRE, mas mapear a categoria
    (evita que apareça em categorias_nao_mapeadas).
"""

import csv
import logging
from io import StringIO
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_COLUNAS_OBRIGATORIAS = {"id_conta", "nivel", "conta_pai", "ordem", "descricao", "sinal"}

_BOOL_MAP = {
    "true": True, "1": True, "sim": True, "yes": True,
    "false": False, "0": False, "nao": False, "não": False, "no": False,
}


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

    return df


def _validar_plano(df: pd.DataFrame, filepath: str) -> None:
    """Loga avisos sobre problemas de preenchimento no plano."""
    nivel3 = df[df["nivel"] == 3]
    sem_cat = nivel3[
        nivel3["categoria_origem"].isna() | (nivel3["categoria_origem"] == "")
    ]
    if not sem_cat.empty:
        exemplos = ", ".join(
            f'"{d}"' for d in sem_cat["descricao"].dropna().tolist()[:5]
        )
        logger.warning(
            "Plano '%s': %d conta(s) nível 3 sem categoria_origem "
            "(não serão mapeadas automaticamente via CSV): %s%s",
            Path(filepath).name,
            len(sem_cat),
            exemplos,
            " …" if len(sem_cat) > 5 else "",
        )


def ler_plano_contas_df(filepath: str) -> pd.DataFrame:
    """Retorna o plano de contas completo como DataFrame tipado."""
    df = _carregar_df(filepath)
    if not df.empty:
        logger.info("Plano de contas: %d conta(s) carregada(s).", len(df))
        _validar_plano(df, filepath)
    return df


def ler_mapeamento_plano(filepath: Optional[str]) -> Dict[str, str]:
    """
    Constrói o mapeamento {categoria_origem → descricao} a partir do CSV.

    Fonte: linhas de nível 3 com categoria_origem preenchida.
    Múltiplas linhas podem ter categoria_origem diferente mas apontar
    para a mesma descricao (conta gerencial).

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

    nivel3 = df[df["nivel"] == 3].copy()
    nivel3_com_cat = nivel3[
        nivel3["categoria_origem"].notna() & (nivel3["categoria_origem"] != "")
    ]

    mapeamento: Dict[str, str] = {}
    n_dre = 0
    n_auditoria = 0

    for _, row in nivel3_com_cat.iterrows():
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
