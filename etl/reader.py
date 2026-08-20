"""
Leitura do Excel do Conta Azul com detecção automática de cabeçalho e
registro de colunas duplicadas renomeadas pelo pandas.
"""

import re
import logging
from typing import Tuple, List

import pandas as pd

logger = logging.getLogger(__name__)

# Termos que indicam que uma linha é o cabeçalho real
_HEADER_KEYWORDS = {"histórico", "historico", "valor (r$)", "data competência",
                    "data competencia", "fornecedor"}


def _find_header_row(df_raw: pd.DataFrame) -> int:
    """Retorna o índice da linha que contém os cabeçalhos reais."""
    for idx, row in df_raw.iterrows():
        values = {str(v).strip().lower() for v in row if pd.notna(v)}
        if values & _HEADER_KEYWORDS:
            return int(idx)
    return 0


def _detect_renamed_duplicates(columns: List[str]) -> List[str]:
    """
    Identifica colunas renomeadas pelo pandas por conta de nomes duplicados.
    Pandas adiciona sufixo `.1`, `.2` etc. na segunda ocorrência em diante.
    """
    avisos = []
    for col in columns:
        match = re.search(r'^(.+)\.(\d+)$', str(col))
        if match:
            original, num = match.group(1), match.group(2)
            aviso = (
                f"Coluna duplicada detectada: '{col}' "
                f"(renomeada pelo pandas de '{original}', ocorrência #{int(num) + 1})"
            )
            avisos.append(aviso)
            logger.warning(aviso)
    return avisos


def read_excel(filepath: str) -> Tuple[pd.DataFrame, List[str]]:
    """
    Lê o arquivo Excel do Conta Azul.

    Retorna:
        df      – DataFrame com os dados (linhas totalmente vazias removidas)
        avisos  – lista de avisos sobre colunas duplicadas ou cabeçalho deslocado
    """
    avisos: List[str] = []

    # Leitura bruta para localizar o cabeçalho
    df_raw = pd.read_excel(filepath, header=None, dtype=str)
    header_row = _find_header_row(df_raw)

    if header_row > 0:
        msg = f"Cabeçalho detectado na linha {header_row + 1} (linhas anteriores ignoradas)."
        logger.info(msg)
        avisos.append(msg)

    df = pd.read_excel(filepath, header=header_row)

    # Remove linhas completamente vazias
    df = df.dropna(how="all").reset_index(drop=True)

    # Detecta e registra colunas com nomes duplicados
    cols = [str(c) for c in df.columns]
    avisos += _detect_renamed_duplicates(cols)

    return df, avisos
