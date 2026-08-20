"""
Resolução de caminhos do projeto ETL.

Hierarquia de busca para cada artefato:
  1. Caminho canônico (cfg/clientes/{cliente}/...)   ← preferido
  2. Caminho legado passado em config.py             ← fallback com aviso
  3. None → feature desabilitada sem erro fatal

Estrutura canônica:
  cfg/
    clientes/
      {cliente}/
        config.yml              ← PDFs por mês (Zeus) ou configurações futuras
        plano_contas_cliente.csv

  data/
    raw/
      {cliente}/
        {mes_ref}/              ← arquivos originais (PDF, XLS)

  saida/
    {cliente}/
      {mes_ref}/                ← CSVs gerados pelo ETL
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Raiz do projeto (dois níveis acima: etl/ → raiz/)
_RAIZ: Path = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Resolvedores públicos
# ---------------------------------------------------------------------------

def resolver_plano_contas(nome_cliente: str, legacy_path: Optional[str] = None) -> Optional[str]:
    """
    Retorna o caminho absoluto do plano de contas do cliente.

    Busca em ordem:
      1. cfg/clientes/{cliente_lower}/plano_contas_cliente.csv
      2. legacy_path (absoluto ou relativo à raiz) — exibe aviso
      3. None (plano de contas desabilitado para este cliente)
    """
    canonico = _RAIZ / "cfg" / "clientes" / nome_cliente.lower() / "plano_contas_cliente.csv"
    if canonico.exists():
        logger.debug("paths: plano_contas '%s' → %s", nome_cliente, canonico)
        return str(canonico)

    if legacy_path:
        p = Path(legacy_path)
        if not p.is_absolute():
            p = _RAIZ / p
        if p.exists():
            _avisar_legado(
                "plano de contas",
                nome_cliente,
                str(p),
                str(canonico),
            )
            return str(p)

    if legacy_path:
        logger.warning(
            "paths: plano_contas '%s' — nenhum arquivo encontrado (canônico=%s | legado=%s).",
            nome_cliente, canonico, legacy_path,
        )
    else:
        logger.debug(
            "paths: plano_contas '%s' — arquivo canônico não encontrado (%s). "
            "Nenhum legado configurado.",
            nome_cliente, canonico,
        )
    return None


def resolver_config_yaml(nome_cliente: str, legacy_path: Optional[str] = None) -> Optional[str]:
    """
    Retorna o caminho absoluto do config.yml do cliente (mapeamento de PDFs, etc.).

    Busca em ordem:
      1. cfg/clientes/{cliente_lower}/config.yml
      2. legacy_path (absoluto ou relativo à raiz) — exibe aviso
      3. None
    """
    canonico = _RAIZ / "cfg" / "clientes" / nome_cliente.lower() / "config.yml"
    if canonico.exists():
        logger.debug("paths: config_yaml '%s' → %s", nome_cliente, canonico)
        return str(canonico)

    if legacy_path:
        p = Path(legacy_path)
        if not p.is_absolute():
            p = _RAIZ / p
        if p.exists():
            _avisar_legado(
                "config YAML",
                nome_cliente,
                str(p),
                str(canonico),
            )
            return str(p)

    return None


def pasta_raw(nome_cliente: str, mes_ref: Optional[str] = None) -> Path:
    """
    Retorna o caminho canônico para os arquivos brutos do cliente.

      data/raw/{cliente}/               (sem mes_ref)
      data/raw/{cliente}/{mes_ref}/     (com mes_ref)
    """
    base = _RAIZ / "data" / "raw" / nome_cliente.lower()
    return base / mes_ref if mes_ref else base


# ---------------------------------------------------------------------------
# Helper interno
# ---------------------------------------------------------------------------

def _avisar_legado(tipo: str, cliente: str, caminho_atual: str, caminho_novo: str) -> None:
    msg = (
        f"⚠  caminho legado detectado — {tipo} do cliente '{cliente}':\n"
        f"   atual : {caminho_atual}\n"
        f"   novo  : {caminho_novo}\n"
        f"   → copie o arquivo para o caminho novo e remova o legado."
    )
    print(f"\n  {msg}")
    logger.warning("paths LEGADO: %s", msg)
