"""
Parser de PDFs Wingraph para Zeus Indústria Gráfica.

Wingraph gera relatórios "Contas a Pagar/Receber por Class. de Conta" em PDF
com layout de colunas fixas (x em pontos PDF, empiricamente medido):

  x <  135  → número do título  (ex.: 01/12, 1.22972-1/1)
  x ≈  139  → data de competência  DD/MM/YYYY
  x ≈  201  → código do fornecedor/cliente  (inteiro)
  x ≈  241  → início do nome do fornecedor/cliente
  x ≈  447  → valor (BRL) — pode estar embutido no nome quando longo
  x ≈  552  → saldo (BRL)

Problema de layout (pdfplumber / PDF gerado pelo Wingraph):
  Quando o nome é longo o suficiente para tocar a coluna de Valor,
  pdfplumber funde as palavras gerando tokens como "SANEAMEN1T45O,57".
  A função _extract_brl_from_concat() resolve isso extraindo somente
  os caracteres numéricos e reconstruindo o padrão BRL correto.
"""

import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

import pdfplumber

logger = logging.getLogger(__name__)

# ─── Limites de colunas (x, em pontos PDF) ──────────────────────────────────
_X_TITULO_MAX  = 135   # número do título: x0 < 135
_X_DATE_MIN    = 134   # data: 134 ≤ x0 ≤ 148
_X_DATE_MAX    = 148
_X_COD_MIN     = 196   # código: 196 ≤ x0 ≤ 215
_X_COD_MAX     = 215
_X_NAME_START  = 238   # nome começa aqui
_X_SALDO_MIN   = 525   # saldo: x0 ≥ 525

# Padrões de texto
_RE_DATE      = re.compile(r'^\d{2}/\d{2}/\d{4}$')
_RE_CAT_CODE  = re.compile(r'^[1-9](?:\.\d{2})+$')  # 2.01.01.01 ou 1.01.02 (1 dígito + grupos de 2)
_RE_COD       = re.compile(r'^\d+$')
_RE_BRL       = re.compile(r'\d{1,3}(?:\.\d{3})*,\d{2}')
# Palavras de cabeçalho / rodapé a ignorar
_SKIP_WORDS   = {"Wingraph", "Subtotal:", "Página", "NCúlamsse.r", "EDmesiscsriãçoão"}


# ─── Funções auxiliares ──────────────────────────────────────────────────────

def _parse_brl(text: str) -> float:
    """'6.229,00' → 6229.0  |  '0,00' → 0.0"""
    clean = text.replace(".", "").replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return 0.0


def _extract_brl_from_concat(words: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    """
    Recebe palavras de uma linha (já sem título/data/cod/saldo limpos).
    Concatena os caracteres numéricos de TODAS as palavras (sem separador)
    e extrai os dois últimos padrões BRL → (valor, saldo).

    Exemplo:
      ["SANEAMEN1T45O,57", "0,00"] → numeric="145,570,00" → ["145,57","0,00"]
    """
    numeric = "".join(re.sub(r"[^0-9,.]", "", w["text"]) for w in words)
    brl_values = _RE_BRL.findall(numeric)
    if len(brl_values) >= 2:
        return brl_values[-2], brl_values[-1]
    if len(brl_values) == 1:
        # Tenta descobrir se é valor ou saldo pela posição x do token
        rightmost_x = max(w["x0"] for w in words) if words else 0
        if rightmost_x >= _X_SALDO_MIN:
            return None, brl_values[0]   # só saldo
        return brl_values[0], None        # só valor
    return None, None


def _group_lines(words: List[dict], y_tol: int = 4) -> List[List[dict]]:
    """Agrupa palavras em linhas por proximidade de y (top)."""
    if not words:
        return []
    lines: List[List[dict]] = []
    current: List[dict] = [words[0]]
    for w in words[1:]:
        if abs(w["top"] - current[0]["top"]) <= y_tol:
            current.append(w)
        else:
            lines.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
    lines.append(sorted(current, key=lambda x: x["x0"]))
    return lines


def _is_header_line(words: List[dict]) -> bool:
    texts = {w["text"] for w in words}
    return bool(texts & _SKIP_WORDS) or any(
        re.search(r"Contas A (pagar|receber) por", w["text"]) for w in words
    )


def _parse_header(words: List[dict]) -> Optional[Tuple[str, str]]:
    """
    Detecta linhas de cabeçalho com intervalo de datas e extrai:
    (mes_referencia '2026-01', tipo_lancamento 'pagar'|'receber')
    """
    full = " ".join(w["text"] for w in words)
    tipo_m = re.search(r"Contas A (pagar|receber)", full, re.IGNORECASE)
    date_m = re.findall(r"\d{2}/\d{2}/\d{4}", full)
    if tipo_m and date_m:
        tipo = tipo_m.group(1).lower()
        # Usa a primeira data do intervalo como referência do mês
        day, month, year = date_m[0].split("/")
        mes = f"{year}-{month}"
        return mes, tipo
    return None


def _parse_category_line(words: List[dict]) -> Optional[Tuple[str, str]]:
    """
    Linha de categoria: primeiro token é código hierárquico (ex.: 2.01.01.01).
    Retorna (codigo, descricao).
    """
    if not words:
        return None
    first = words[0]
    if first["x0"] < 25 and _RE_CAT_CODE.match(first["text"]):
        descricao = " ".join(w["text"] for w in words[1:]).strip()
        return first["text"], descricao
    return None


def _parse_transaction_line(words: List[dict]) -> Optional[dict]:
    """
    Linha de lançamento: contém uma data DD/MM/YYYY.
    Retorna dict com campos brutos ou None se não reconhecida.
    """
    # Localiza a data
    date_word = next(
        (w for w in words if _X_DATE_MIN <= w["x0"] <= _X_DATE_MAX and _RE_DATE.match(w["text"])),
        None,
    )
    if not date_word:
        return None

    # Número do título: palavras antes da data
    titulo_words = [w for w in words if w["x0"] < _X_TITULO_MAX]
    numero_titulo = " ".join(w["text"] for w in titulo_words).strip()

    # Código do fornecedor
    cod_word = next(
        (w for w in words if _X_COD_MIN <= w["x0"] <= _X_COD_MAX and _RE_COD.match(w["text"])),
        None,
    )

    # Saldo: última palavra ao extremo direito (x ≥ _X_SALDO_MIN) que seja BRL
    saldo_word = None
    clean_saldo = None
    saldo_candidates = [w for w in words if w["x0"] >= _X_SALDO_MIN]
    for cw in reversed(saldo_candidates):
        if _RE_BRL.fullmatch(cw["text"]):
            saldo_word = cw
            clean_saldo = cw["text"]
            break

    # Valor: palavra BRL imediatamente antes do saldo, na zona de valor
    # (x ≥ _X_NAME_START, x < _X_SALDO_MIN) — pode ser limpa ou embutida
    x_after_cod = (cod_word["x0"] + 20) if cod_word else _X_NAME_START
    remaining_words = [
        w for w in words
        if w["x0"] >= x_after_cod
        and w is not date_word
        and w is not cod_word
        and w is not saldo_word
    ]

    # Tenta extrair valor e saldo a partir das palavras restantes
    # (funciona para clean e garbled)
    valor_str, saldo_str_fallback = _extract_brl_from_concat(remaining_words)

    if clean_saldo is not None:
        # Saldo extraído limpo da zona de saldo.
        # _extract_brl_from_concat operou sobre remaining (sem o saldo_word).
        # Se retornou apenas 1 BRL (no "slot de saldo"), é na verdade o valor.
        if valor_str is None and saldo_str_fallback is not None:
            valor_str = saldo_str_fallback
    else:
        clean_saldo = saldo_str_fallback

    # Nome do fornecedor: palavras no intervalo de nome, limpando tokens de valor embutido
    name_words = [w for w in remaining_words if w["x0"] < _X_SALDO_MIN]
    # Remove tokens que SÃO puramente BRL (valor limpo sem texto)
    name_parts = []
    for w in name_words:
        if _RE_BRL.fullmatch(w["text"]):
            break  # chegou na coluna de valor, para
        # Limpa dígitos de tokens garblados para exibir o nome humanamente legível
        cleaned_name_token = re.sub(r"\d", "", w["text"]).replace(",", "").replace(".", "").strip()
        if cleaned_name_token:
            name_parts.append(cleaned_name_token)
    fornecedor = " ".join(name_parts).strip()

    return {
        "numero_titulo":     numero_titulo,
        "data_competencia":  date_word["text"],
        "cod_fornecedor":    cod_word["text"] if cod_word else None,
        "fornecedor_cliente": fornecedor,
        "valor_str":         valor_str,
        "saldo_str":         clean_saldo,
    }


# ─── Parser principal ─────────────────────────────────────────────────────────

def parse_wingraph_pdf(
    pdf_path: str,
    nome_cliente: str = "Zeus",
    empresa_origem: str = "",
) -> List[dict]:
    """
    Lê um PDF Wingraph (AP ou AR) e retorna lista de lançamentos.

    Campos retornados por lançamento:
      cliente, mes_referencia, tipo_lancamento, data_competencia,
      numero_titulo, cod_fornecedor, fornecedor_cliente, descricao,
      categoria_codigo, categoria_origem, valor, saldo, id_linha_origem,
      status_validacao, observacao_etl
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF não encontrado: {pdf_path}")

    lancamentos: List[dict] = []
    mes_referencia: Optional[str] = None
    tipo_lancamento: Optional[str] = None
    categoria_codigo: Optional[str] = None
    categoria_origem: Optional[str] = None
    ultimo_lancamento: Optional[dict] = None
    id_linha = 0

    with pdfplumber.open(pdf_path) as pdf:
        logger.info("Zeus reader: '%s' — %d página(s)", path.name, len(pdf.pages))

        for page_num, page in enumerate(pdf.pages, 1):
            words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False)
            lines = _group_lines(words)

            for line_words in lines:
                if not line_words:
                    continue

                full_text = " ".join(w["text"] for w in line_words)

                # 1. Ignora linhas vazias / muito curtas
                if len(full_text.strip()) < 3:
                    continue

                # 2. Detecta cabeçalho (extrai mes_referencia e tipo_lancamento)
                header = _parse_header(line_words)
                if header:
                    mes_referencia, tipo_lancamento = header
                    logger.info("Zeus reader: página %d — %s / %s", page_num, mes_referencia, tipo_lancamento)
                    continue

                # 3. Ignora linhas de paginação / subtotais / coluna-header
                if _is_header_line(line_words):
                    continue
                if "Subtotal:" in full_text:
                    continue

                # 4. Categoria
                cat = _parse_category_line(line_words)
                if cat:
                    categoria_codigo, categoria_origem = cat
                    ultimo_lancamento = None   # nova categoria, sem continuação pendente
                    continue

                # 5. Lançamento
                tx = _parse_transaction_line(line_words)
                if tx:
                    id_linha += 1
                    valor_raw = tx["valor_str"] or "0,00"
                    saldo_raw = tx["saldo_str"] or "0,00"
                    valor_float = _parse_brl(valor_raw)

                    # Despesas (pagar) são negativas; receitas (receber) são positivas
                    if tipo_lancamento == "pagar":
                        valor_float = -abs(valor_float)

                    observacoes = []
                    if tx["valor_str"] is None:
                        observacoes.append("valor não extraído — linha possivelmente garblada")

                    rec = {
                        "cliente":            nome_cliente,
                        "empresa_origem":     empresa_origem,
                        "mes_referencia":     mes_referencia,
                        "tipo_lancamento":    tipo_lancamento,
                        "data_competencia":   tx["data_competencia"],
                        "numero_titulo":      tx["numero_titulo"],
                        "cod_fornecedor":     tx["cod_fornecedor"],
                        "fornecedor_cliente": tx["fornecedor_cliente"],
                        "descricao":          "",           # preenchido por linhas de continuação
                        "categoria_codigo":   categoria_codigo,
                        "categoria_origem":   categoria_origem,
                        "valor":              round(valor_float, 2),
                        "saldo":              _parse_brl(saldo_raw),
                        "id_linha_origem":    id_linha,
                        "status_validacao":   "ok" if tx["valor_str"] else "revisar",
                        "observacao_etl":     "; ".join(observacoes) if observacoes else None,
                    }
                    lancamentos.append(rec)
                    ultimo_lancamento = rec
                    continue

                # 6. Linha de continuação (descrição adicional)
                # Não começa com data, não é categoria, não é cabeçalho
                if ultimo_lancamento is not None:
                    desc_text = full_text.strip()
                    # Ignora linhas que claramente são lixo de paginação
                    if desc_text and not re.match(r"^\d{1,2}$", desc_text):
                        sep = " | " if ultimo_lancamento["descricao"] else ""
                        ultimo_lancamento["descricao"] += sep + desc_text

    logger.info("Zeus reader: %d lançamento(s) extraído(s) de '%s'", len(lancamentos), path.name)
    return lancamentos
