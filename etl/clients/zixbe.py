"""
Processador Zixbe — Conta Azul visão de competência.

Estrutura de blocos por categoria
----------------------------------
O Conta Azul exporta um bloco de colunas para cada categoria:

  Categoria 1 | Valor na Categoria 1 | CC 1..6 | Valor no CC 1..6
  Categoria 2 | Valor na Categoria 2 | CC 1    | Valor no CC 1
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^
                              pandas renomeia para CC 1.1 / Valor no CC 1.1
                              porque os nomes originais já foram usados no bloco 1

Regra de expansão por bloco
-----------------------------
  Para cada Categoria N com valor ≠ 0:
    - Se o bloco de CC tiver valores → uma linha por CC (categoria fixa)
    - Senão                          → uma linha com o valor da categoria

Validação: soma dos valores gerados deve bater com Valor (R$) original (tolerância R$0,01).
"""

import re
import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config import TOLERANCIA_CENTAVOS
from etl.clients.base import ProcessadorBase, ResultadoETL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversão de valores brasileiros
# ---------------------------------------------------------------------------

def parse_brl_number(val: Any) -> Optional[float]:
    """
    Converte valor numérico no formato brasileiro (ou já numérico) para float.

    Regras:
      - int / float já prontos  → retorna como float (sem tocar)
      - NaN / None / string vazia → None
      - String com vírgula       → remove pontos de milhar, troca vírgula por ponto
      - String só com ponto      → trata ponto como decimal (formato neutro/US)
      - Preserva sinal negativo em qualquer formato
    """
    if val is None:
        return None
    if isinstance(val, float):
        return None if pd.isna(val) else val
    if isinstance(val, int):
        return float(val)

    s = str(val).strip()
    if not s:
        return None

    if "," in s:
        # Formato brasileiro: ponto = milhar, vírgula = decimal
        # "15.000,00" → "15000.00"   "-922,50" → "-922.50"
        s = s.replace(".", "").replace(",", ".")

    try:
        return float(s)
    except ValueError:
        logger.warning("Não foi possível converter '%s' para número.", val)
        return None


# ---------------------------------------------------------------------------
# Construção dos blocos de categoria
# ---------------------------------------------------------------------------

def _parse_col_suffix(col: str, prefix: str) -> Optional[Tuple[str, int]]:
    """
    Extrai (numero_original, bloco) do sufixo de uma coluna dinâmica.

    "Centro de Custo 1"   → ("1", 0)   – bloco 0, CC nº 1
    "Centro de Custo 2"   → ("2", 0)   – bloco 0, CC nº 2
    "Centro de Custo 1.1" → ("1", 1)   – bloco 1 (Categoria 2), CC nº 1
    "Centro de Custo 2.1" → ("2", 1)   – bloco 1, CC nº 2
    """
    suffix = str(col)[len(prefix):].strip()
    m = re.match(r'^(\d+)(?:\.(\d+))?$', suffix)
    if not m:
        return None
    num = m.group(1)
    block = int(m.group(2)) if m.group(2) else 0
    return num, block


def _build_category_blocks(columns: List[str], prefixos: dict) -> List[dict]:
    """
    Retorna lista de blocos, um por Categoria N, na ordem numérica:

      {
        "cat_col":     "Categoria 1",
        "val_cat_col": "Valor na Categoria 1",
        "cc_pairs":    [("Centro de Custo 1", "Valor no Centro de Custo 1"), ...]
      }

    Bloco 0 → Categoria 1, usa CC sem sufixo pandas (.N)
    Bloco 1 → Categoria 2, usa CC com sufixo ".1"
    Bloco N → Categoria N+1, usa CC com sufixo ".N"
    """
    cat_pfx     = prefixos["categoria"]
    val_cat_pfx = prefixos["valor_categoria"]
    cc_pfx      = prefixos["centro_custo"]
    val_cc_pfx  = prefixos["valor_centro_custo"]

    # Categoria columns: "Categoria 1", "Categoria 2", … (sempre únicos, sem sufixo pandas)
    cat_cols = sorted(
        [c for c in columns if re.match(r'^' + re.escape(cat_pfx) + r'\s+\d+$', str(c))],
        key=lambda c: int(re.search(r'\d+$', str(c)).group()),
    )

    # Index value-CC columns: (num, block) → col_name
    val_cc_idx: Dict[Tuple[str, int], str] = {}
    for col in columns:
        if str(col).startswith(val_cc_pfx + " "):
            info = _parse_col_suffix(str(col), val_cc_pfx + " ")
            if info:
                val_cc_idx[info] = col

    # Group CC name columns by block
    cc_by_block: Dict[int, List[Tuple[str, Optional[str]]]] = {}
    for col in columns:
        s = str(col)
        if s.startswith(cc_pfx + " ") and not s.startswith(val_cc_pfx + " "):
            info = _parse_col_suffix(s, cc_pfx + " ")
            if info:
                num, block = info
                val_col = val_cc_idx.get((num, block))
                cc_by_block.setdefault(block, []).append((col, val_col))

    # Sort CCs within each block by their original number
    for block in cc_by_block:
        cc_by_block[block].sort(
            key=lambda pair: int(re.search(r'^\d+', str(pair[0])[len(cc_pfx):].strip()).group())
        )

    # Assemble final block list
    blocks = []
    for i, cat_col in enumerate(cat_cols):
        num = re.search(r'\d+$', str(cat_col)).group()
        val_cat_col = f"{val_cat_pfx} {num}"
        blocks.append({
            "cat_col":     cat_col,
            "val_cat_col": val_cat_col if val_cat_col in columns else None,
            "cc_pairs":    cc_by_block.get(i, []),
        })

    return blocks


# ---------------------------------------------------------------------------
# Processador principal
# ---------------------------------------------------------------------------

class ZixbeProcessor(ProcessadorBase):

    def processar(self, df: pd.DataFrame) -> ResultadoETL:
        resultado = ResultadoETL()
        columns  = [str(c) for c in df.columns]
        cfg      = self.config
        fixas    = cfg["colunas_fixas"]
        mapeamento = cfg.get("mapeamento_categorias", {})

        blocks = _build_category_blocks(columns, cfg["prefixos"])
        categorias_encontradas: set = set()

        for idx, row in df.iterrows():
            id_linha = idx + 1

            data_raw = row.get(fixas["data_competencia"])
            try:
                data_competencia = pd.to_datetime(data_raw, dayfirst=True).date()
                mes_referencia   = data_competencia.strftime("%Y-%m")
            except Exception:
                data_competencia = data_raw
                mes_referencia   = None

            descricao  = str(row.get(fixas["descricao"], "")).strip() or None
            fornecedor = str(row.get(fixas["fornecedor_cliente"], "")).strip() or None
            valor_total = parse_brl_number(row.get(fixas["valor_total"]))

            if valor_total is None:
                logger.debug("Linha %d: 'Valor (R$)' ausente, ignorada.", id_linha)
                continue

            linhas_geradas = self._expand_row(row, blocks)

            for item in linhas_geradas:
                if item["categoria_origem"]:
                    categorias_encontradas.add(item["categoria_origem"])

            soma   = sum(item["valor"] for item in linhas_geradas)
            status = "ok"
            obs    = None

            if abs(soma - valor_total) > TOLERANCIA_CENTAVOS:
                status = "divergencia"
                obs = (
                    f"Soma gerada R${soma:.2f} ≠ valor original R${valor_total:.2f} "
                    f"(Δ={soma - valor_total:+.2f})"
                )
                resultado.inconsistencias.append({
                    "id_linha_origem":       id_linha,
                    "data_competencia":      data_competencia,
                    "descricao":             descricao,
                    "fornecedor_cliente":    fornecedor,
                    "valor_total_lancamento": valor_total,
                    "soma_valores_gerados":  round(soma, 2),
                    "diferenca":             round(soma - valor_total, 2),
                    "observacao":            obs,
                })

            for item in linhas_geradas:
                resultado.lancamentos.append({
                    "cliente":                cfg["nome"],
                    "mes_referencia":         mes_referencia,
                    "data_competencia":       data_competencia,
                    "descricao":              descricao,
                    "fornecedor_cliente":     fornecedor,
                    "categoria_origem":       item["categoria_origem"],
                    "valor":                  item["valor"],
                    "centro_custo":           item["centro_custo"],
                    "valor_total_lancamento": valor_total,
                    "id_linha_origem":        id_linha,
                    "status_validacao":       status,
                    "observacao_etl":         obs,
                })

        for cat in sorted(categorias_encontradas):
            if cat not in mapeamento:
                resultado.categorias_nao_mapeadas.append({
                    "categoria_original": cat,
                    "categoria_mapeada":  mapeamento.get(cat),
                    "situacao":           "nao_mapeada",
                })

        return resultado

    # ------------------------------------------------------------------

    def _expand_row(self, row: pd.Series, blocks: List[dict]) -> List[dict]:
        """
        Expande uma linha em registros de saída respeitando blocos por categoria.

        Para cada bloco (Categoria N):
          - Se o bloco de CC tiver valores → uma linha por CC (mesma categoria)
          - Senão                          → uma linha com o valor da categoria
        """
        result = []

        for block in blocks:
            cat_name = row.get(block["cat_col"])
            if pd.isna(cat_name) or str(cat_name).strip() == "":
                continue
            cat_name = str(cat_name).strip()

            cat_val = parse_brl_number(row.get(block["val_cat_col"])) if block["val_cat_col"] else None
            if cat_val is None or cat_val == 0.0:
                continue

            cc_with_values = []
            for cc_col, val_cc_col in block["cc_pairs"]:
                cc_name = row.get(cc_col)
                if pd.isna(cc_name) or str(cc_name).strip() == "":
                    continue
                cc_val = parse_brl_number(row.get(val_cc_col)) if val_cc_col else None
                if cc_val is not None and cc_val != 0.0:
                    cc_with_values.append((str(cc_name).strip(), cc_val))

            if cc_with_values:
                for cc_name, cc_val in cc_with_values:
                    result.append({"categoria_origem": cat_name, "valor": cc_val, "centro_custo": cc_name})
            else:
                result.append({"categoria_origem": cat_name, "valor": cat_val, "centro_custo": None})

        if not result:
            result.append({"categoria_origem": None, "valor": 0.0, "centro_custo": None})

        return result
