"""
Resolução de mapeamentos de categorias por cliente.

Fluxo de prioridade (maior → menor):
  1. config.py  → mapeamento_categorias  (overrides manuais explícitos)
  2. Supabase   → mapeamentos_cliente    (frontend / Lovable)
  3. CSV        → plano_contas_cliente   (estrutura base)
  ─────────────────────────────────────────────────────
  4. Não mapeada → categorias_nao_mapeadas.csv

Como o DRE usa as categorias:
  gerar_dre() agrupa lançamentos por `categoria_origem` e faz match
  contra `plano_df["descricao"]` em nível 3.
  Portanto, o `descricao_conta` de cada mapeamento Supabase DEVE ser
  exatamente igual a uma descrição de nível 3 do plano_contas_cliente.csv.
  Se não for, o valor do lançamento soma zero no DRE (perde silenciosamente).

Ciclo de reprocessamento:
  ETL roda → gera categorias_nao_mapeadas.csv
  → Lovable salva entradas em mapeamentos_cliente (descricao_conta = nivel-3)
  → ETL reprocessa com --overwrite
  → categorias agora classificadas e visíveis no DRE
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Carregamento do Supabase
# ---------------------------------------------------------------------------

def carregar_mapeamentos_supabase(nome_cliente: str) -> Dict[str, dict]:
    """
    Busca mapeamentos ativos em mapeamentos_cliente para o cliente.

    Retorna dict:
        {categoria_origem: {
            "descricao_conta": str,
            "id_conta": str | None,
            "nivel_1": str | None,
            "nivel_2": str | None,
            "nivel_3": str | None,
            "sinal": int,
        }}

    Retorna {} silenciosamente se:
      - variáveis Supabase não configuradas
      - tabela mapeamentos_cliente não existe ainda
      - qualquer erro de conexão
    """
    try:
        from src.supabase_client import buscar_cliente_id, _get_client
        cliente_id = buscar_cliente_id(nome_cliente)
        if not cliente_id:
            logger.debug("mapeamentos: cliente_id não encontrado para '%s', ignorando.", nome_cliente)
            return {}

        client = _get_client()
        res = (
            client.table("mapeamentos_cliente")
            .select("categoria_origem,id_conta,descricao_conta,nivel_1,nivel_2,nivel_3,sinal")
            .eq("cliente_id", cliente_id)
            .eq("ativo", True)
            .execute()
        )

        resultado: Dict[str, dict] = {}
        for r in res.data:
            resultado[r["categoria_origem"]] = {
                "descricao_conta": r["descricao_conta"],
                "id_conta":        r.get("id_conta"),
                "nivel_1":         r.get("nivel_1"),
                "nivel_2":         r.get("nivel_2"),
                "nivel_3":         r.get("nivel_3"),
                "sinal":           r.get("sinal", 1),
            }

        logger.info(
            "mapeamentos_cliente: %d mapeamento(s) ativo(s) para '%s'.",
            len(resultado), nome_cliente,
        )
        return resultado

    except EnvironmentError:
        logger.debug("mapeamentos: Supabase não configurado — usando apenas plano_contas CSV.")
        return {}

    except Exception as exc:
        _msg = str(exc)
        if "does not exist" in _msg or "relation" in _msg.lower():
            logger.warning(
                "mapeamentos_cliente: tabela ainda não existe no Supabase. "
                "Execute o DDL em supabase_schema.sql para criá-la."
            )
        else:
            logger.warning(
                "mapeamentos_cliente: não foi possível carregar para '%s' (%s). "
                "Usando apenas plano_contas CSV.", nome_cliente, exc,
            )
        return {}


# ---------------------------------------------------------------------------
# Construção do mapeamento final
# ---------------------------------------------------------------------------

def construir_mapeamento_final(
    mapeamentos_supabase: Dict[str, dict],
    mapeamento_plano: Dict[str, str],
    mapeamento_manual: Dict[str, str],
) -> Dict[str, str]:
    """
    Combina as três fontes em ordem de prioridade crescente:

        final = plano_CSV  ←  Supabase  ←  manual

    Cada nível sobrescreve o anterior.
    Retorna {categoria_origem: descricao_conta_final}.

    Nota: o valor final DEVE ser uma descrição de nível 3 do plano_contas
    para aparecer no DRE. Use validar_categorias_dre() para verificar.
    """
    final: Dict[str, str] = {}
    final.update(mapeamento_plano)   # base: {categoria_origem_ERP: descricao_nivel3} — do CSV
    final.update({                   # Supabase override: categoria_origem → descricao_conta
        k: v["descricao_conta"]
        for k, v in mapeamentos_supabase.items()
    })
    final.update(mapeamento_manual)  # manual: máxima prioridade
    return final


# ---------------------------------------------------------------------------
# Relatório e validação
# ---------------------------------------------------------------------------

def imprimir_relatorio_mapeamento(
    categorias_nao_mapeadas: List[dict],
    mapeamentos_supabase: Dict[str, dict],
    mapeamento_plano: Dict[str, str],
    mapeamento_manual: Dict[str, str],
) -> None:
    """
    Imprime resumo das fontes de mapeamento e detalha cada entrada Supabase.
    """
    n_supabase = len(mapeamentos_supabase)
    n_plano    = len(mapeamento_plano)
    n_manual   = len(mapeamento_manual)
    n_nao_map  = len(categorias_nao_mapeadas)

    sep = "-" * 60
    print(f"\n{sep}")
    print("  Fontes de mapeamento de categorias:")
    print(f"    via mapeamentos_cliente (Supabase): {n_supabase:>4} entrada(s)")
    print(f"    via plano_contas CSV:               {n_plano:>4} entrada(s)")
    if n_manual:
        print(f"    via config manual:                  {n_manual:>4} entrada(s)")
    print(f"    categorias não mapeadas:            {n_nao_map:>4}")

    # Detalha cada mapeamento Supabase (o que o Lovable configurou)
    if mapeamentos_supabase:
        print(f"\n  Mapeamentos via Supabase (categoria_origem → descricao_conta):")
        for orig, dados in sorted(mapeamentos_supabase.items()):
            dest    = dados["descricao_conta"]
            id_cont = dados.get("id_conta") or ""
            sufixo  = f"  [id_conta={id_cont}]" if id_cont else ""
            print(f"    {orig!r:45} → {dest!r}{sufixo}")
            logger.info("  Supabase: %r → %r (id_conta=%s)", orig, dest, id_cont or "—")
    else:
        print("  ⚠  Nenhum mapeamento Supabase ativo para este cliente.")
        print("     → Verifique se mapeamentos_cliente foi preenchido no Lovable.")

    if n_nao_map:
        print(f"\n  ⚠  {n_nao_map} categoria(s) sem mapeamento → veja categorias_nao_mapeadas.csv")
        print("     Configure-as no Lovable para incluí-las no DRE.")

    print(sep)

    logger.info(
        "mapeamentos: manual=%d | supabase=%d | plano=%d | nao_mapeadas=%d",
        n_manual, n_supabase, n_plano, n_nao_map,
    )
    for cat in sorted(c.get("categoria_original", "") for c in categorias_nao_mapeadas):
        if cat:
            logger.info("  não mapeada: %r", cat)


def validar_categorias_dre(
    lancamentos: List[dict],
    nivel3_validas: set,
    mapeamentos_supabase: Dict[str, dict],
) -> None:
    """
    Verifica se os valores finais de categoria_origem nos lançamentos
    correspondem a descrições de nível 3 do plano que contribuem para o DRE
    (exibir_dre=True).

    Categorias que NÃO estiverem em nivel3_validas serão ignoradas por
    gerar_dre() e seus valores somam zero no DRE — silenciosamente.

    Args:
        lancamentos:      lista de lançamentos após mapeamento (categoria_origem = descricao final)
        nivel3_validas:   set de descrições de nível 3 com exibir_dre=True do plano
        mapeamentos_supabase: mapeamentos ativos no Supabase (para diagnóstico de origem)
    """
    if not lancamentos:
        return

    # Valores de descricao_conta configurados via Supabase
    supabase_destinos: Dict[str, str] = {
        v["descricao_conta"]: orig
        for orig, v in mapeamentos_supabase.items()
    }

    # Categorias únicas presentes nos lançamentos após mapeamento
    cats_final = {
        l.get("categoria_origem")
        for l in lancamentos
        if l.get("categoria_origem")
    }

    contrib   = sorted(cats_final & nivel3_validas)
    nao_contrib = sorted(cats_final - nivel3_validas)

    sep = "-" * 60
    print(f"\n{sep}")
    print("  Validação DRE — categoria_origem vs nível-3 do plano:")
    print(f"    contribuem para o DRE:     {len(contrib):>4}")
    print(f"    NÃO contribuem (sem nível-3): {len(nao_contrib):>4}")

    if nao_contrib:
        print(f"\n  ⚠  Categorias que NÃO aparecem no DRE (não encontradas em nível-3):")
        for cat in nao_contrib:
            # Identifica se veio do Supabase (para apontar a correção necessária)
            if cat in supabase_destinos:
                orig = supabase_destinos[cat]
                print(
                    f"    ⚠  {cat!r}\n"
                    f"       ← Supabase mapeou {orig!r} para este valor,\n"
                    f"          mas {cat!r} NÃO existe em nível-3 no plano_contas_cliente.csv.\n"
                    f"          Corrija descricao_conta no Lovable para um valor de nível-3 válido."
                )
                logger.warning(
                    "DRE: Supabase mapeou %r → %r, mas %r não é nível-3 no plano. "
                    "Valor perdido no DRE.", orig, cat, cat,
                )
            else:
                print(f"    ⚠  {cat!r}  (não mapeada a nenhum nível-3)")
                logger.warning("DRE: categoria %r não encontrada em nível-3 do plano.", cat)
    else:
        print("  ✓ Todas as categorias estão mapeadas a nível-3 → DRE completo.")

    if contrib:
        print(f"\n  Categorias que contribuem para o DRE ({len(contrib)}):")
        for cat in contrib:
            from_sb = " [via Supabase]" if cat in supabase_destinos else ""
            print(f"    ✓  {cat}{from_sb}")

    print(sep)

    logger.info(
        "DRE validação: %d contribuem | %d perdidas",
        len(contrib), len(nao_contrib),
    )
