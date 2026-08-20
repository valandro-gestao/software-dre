"""
Orquestra leitura → processamento → escrita dos CSVs de saída.

Estrutura canônica do projeto:
    cfg/clientes/{cliente}/
        config.yml                ← PDFs por mês (Zeus) e configs futuras
        plano_contas_cliente.csv  ← plano de contas

    data/raw/{cliente}/{mes_ref}/ ← arquivos brutos de entrada (PDF, XLS)

    saida/{cliente}/{mes_ref}/    ← CSVs gerados pelo ETL
        lancamentos_normalizados.csv
        dre_mensal.csv
        resumo_conferencia.csv
        resumo_auditoria.csv      (apenas para clientes multi-CNPJ, ex.: Zeus)
        inconsistencias.csv
        categorias_nao_mapeadas.csv
"""

import logging
import sys
from pathlib import Path

import pandas as pd

from config import CLIENTES
from etl.reader import read_excel
from etl.clients.base import ResultadoETL
from etl.paths import resolver_plano_contas
from etl.plano_contas import ler_plano_contas_df, ler_mapeamento_plano
from etl.dre import gerar_dre
from etl.mapeamentos import (
    carregar_mapeamentos_supabase,
    construir_mapeamento_final,
    imprimir_relatorio_mapeamento,
    validar_categorias_dre,
)

logger = logging.getLogger(__name__)

_PROCESSADORES = {
    "zixbe": "etl.clients.zixbe.ZixbeProcessor",
    "zeus":  "etl.clients.zeus.ZeusProcessor",
}


def _carregar_processador(nome_cliente: str, config: dict):
    caminho = _PROCESSADORES.get(nome_cliente)
    if not caminho:
        raise ValueError(f"Nenhum processador registrado para o cliente '{nome_cliente}'.")
    modulo_path, classe_nome = caminho.rsplit(".", 1)
    import importlib
    modulo = importlib.import_module(modulo_path)
    cls = getattr(modulo, classe_nome)
    return cls(config)


def _gerar_resumo(df_original: pd.DataFrame, resultado: ResultadoETL, config: dict) -> list:
    """
    Gera o resumo de conferência.
    - Clientes Excel (Zixbe): compara total do arquivo original com o ETL.
    - Clientes PDF (Zeus): usa o total dos lançamentos como referência.
    """
    total_etl = sum(l["valor"] for l in resultado.lancamentos)

    if "colunas_fixas" in config:
        # Fonte Excel: confronta com o arquivo original
        valor_col    = config["colunas_fixas"]["valor_total"]
        total_original = float(df_original[valor_col].dropna().sum())
        qtd_origem   = len(df_original)
    else:
        # Fonte PDF: total_original = total dos lançamentos (sem diferença esperada)
        total_original = total_etl
        qtd_origem   = len(resultado.lancamentos)

    return [{
        "total_original":      round(total_original, 2),
        "total_etl":           round(total_etl, 2),
        "diferenca":           round(total_etl - total_original, 2),
        "qtd_linhas_origem":   qtd_origem,
        "qtd_linhas_saida":    len(resultado.lancamentos),
        "qtd_inconsistencias": len(resultado.inconsistencias),
        "qtd_nao_mapeadas":    len(resultado.categorias_nao_mapeadas),
    }]


def _formatar_brl(v) -> str:
    """Converte float para string no formato brasileiro: 15000.0 → '15000,00'."""
    return f"{v + 0.0:.2f}".replace(".", ",")  # +0.0 converte -0.0 → 0.0


def _salvar_csv(registros: list, caminho: Path, descricao: str) -> None:
    if not registros:
        logger.info("%s: sem registros, CSV não gerado.", descricao)
        return
    df = pd.DataFrame(registros)
    for col in df.select_dtypes(include=["float64", "float32"]).columns:
        df[col] = df[col].apply(lambda v: _formatar_brl(v) if pd.notna(v) else "")
    df.to_csv(caminho, index=False, encoding="utf-8-sig", sep=";")
    logger.info(f"{descricao}: {len(df)} registro(s) → {caminho}")
    print(f"  [{len(df):>5} registros]  {caminho.name}")


def run_pipeline(
    input_file: str,
    nome_cliente: str,
    output_dir: str,
    save_supabase: bool = False,
    mes_ref: str = None,
    overwrite: bool = False,
) -> None:
    # ── Checkpoint 2: valor recebido do main.py ───────────────────────────
    print(f"CLIENTE RECEBIDO NO PIPELINE: {nome_cliente!r}")

    nome_cliente = nome_cliente.lower()
    config = CLIENTES.get(nome_cliente)
    if not config:
        clientes_disponiveis = ", ".join(CLIENTES.keys())
        sys.exit(f"Cliente '{nome_cliente}' não configurado. Disponíveis: {clientes_disponiveis}")

    # ── Checkpoint 3: nome canônico que virá do config ────────────────────
    _nome_config = config["nome"]
    print(f"NOME DO CLIENTE NO CONFIG:    {_nome_config!r}  (chave CLIENTES={nome_cliente!r})")

    # Guarda defensiva: nome do config deve corresponder à chave CLI
    if _nome_config.lower() != nome_cliente.lower():
        sys.exit(
            f"INCONSISTÊNCIA CRÍTICA: --client={nome_cliente!r} mas config['nome']={_nome_config!r}. "
            "Verifique config.py."
        )

    # ── Estrutura de saída: {output_dir}/{cliente}/{mes_ref}/ ──────────────
    mes_label   = mes_ref or "sem-mes"
    output_path = Path(output_dir) / nome_cliente / mes_label
    output_path.mkdir(parents=True, exist_ok=True)
    # Arquivos brutos ficam em data/raw/{cliente}/{mes_ref}/ — não em saida/
    print(f"\n  Saída:    {output_path}")
    from etl.paths import pasta_raw
    print(f"  Brutos:   {pasta_raw(nome_cliente, mes_label)}  (coloque aqui os PDFs/XLS originais)")

    # ── Injeta mes_ref no config para processadores que precisam ───────────
    config = {**config, "mes_ref": mes_ref}

    # ── Leitura (apenas para clientes Excel) ──────────────────────────────
    eh_pdf = config.get("tipo_fonte") == "pdf"
    df     = pd.DataFrame()

    if not eh_pdf:
        if not input_file:
            sys.exit("Erro: caminho do arquivo Excel é obrigatório para este cliente.")
        print(f"\nLendo arquivo: {input_file}")
        df, avisos = read_excel(input_file)
        if avisos:
            print("  Avisos de leitura:")
            for a in avisos:
                print(f"    · {a}")
        print(f"  {len(df)} linha(s) carregada(s).\n")

    # ── Plano de contas — resolve caminho canônico com fallback legado ────
    plano_path = resolver_plano_contas(
        _nome_config,
        legacy_path=config.get("plano_contas_legado"),
    )
    plano_df   = ler_plano_contas_df(plano_path) if plano_path else pd.DataFrame()

    # Mapeamento base: {categoria_origem_ERP → descricao_nivel3} via coluna categoria_origem do CSV.
    # Inclui auditoria_only para não aparecerem em categorias_nao_mapeadas.
    mapeamento_plano = ler_mapeamento_plano(plano_path)

    # Subset do plano que efetivamente contribui ao DRE (exibir_dre=True).
    if not plano_df.empty:
        _mask_dre = plano_df["exibir_dre"].fillna(True).astype(bool)
        plano_dre = plano_df[_mask_dre].copy()
        nivel3_validas: set = set(
            plano_dre[plano_dre["nivel"] == 3]["descricao"].dropna()
        )
        n_auditoria = int((plano_df["nivel"] == 3).sum()) - int((plano_dre["nivel"] == 3).sum())
    else:
        plano_dre = pd.DataFrame()
        nivel3_validas = set()
        n_auditoria = 0

    if mapeamento_plano:
        print(
            f"  Plano de contas CSV: {len(mapeamento_plano)} categoria(s) mapeada(s) "
            f"via categoria_origem"
            + (f" ({n_auditoria} auditoria_only, fora do DRE)." if n_auditoria else ".")
        )

    # ── Mapeamentos dinâmicos do Supabase (mapeamentos_cliente) ──────────
    # Carregados sempre que Supabase estiver configurado, independente de
    # --save-supabase. Se a tabela não existir ainda, retorna {} com aviso.
    _mapeamentos_supabase = carregar_mapeamentos_supabase(_nome_config)
    if _mapeamentos_supabase:
        print(f"  Mapeamentos Supabase: {len(_mapeamentos_supabase)} entrada(s) ativa(s).")

    # ── Mapeamento final: plano CSV ← Supabase ← config manual ───────────
    _mapeamento_manual = config.get("mapeamento_categorias", {})
    mapeamento_final = construir_mapeamento_final(
        mapeamentos_supabase=_mapeamentos_supabase,
        mapeamento_plano=mapeamento_plano,
        mapeamento_manual=_mapeamento_manual,
    )
    config = {**config, "mapeamento_categorias": mapeamento_final}

    # ── Processamento ─────────────────────────────────────────────────────
    print(f"\nProcessando cliente: {config['nome']} …")
    processador = _carregar_processador(nome_cliente, config)
    resultado: ResultadoETL = processador.processar(df)

    # ── Relatório de mapeamento de categorias ─────────────────────────────
    imprimir_relatorio_mapeamento(
        categorias_nao_mapeadas=resultado.categorias_nao_mapeadas,
        mapeamentos_supabase=_mapeamentos_supabase,
        mapeamento_plano=mapeamento_plano,
        mapeamento_manual=_mapeamento_manual,
    )

    # ── Validação DRE: categorias mapeadas vs nível-3 com exibir_dre=True ─
    # Mostra quais categoria_origem dos lançamentos contribuirão ao DRE e quais
    # serão silenciosamente ignoradas (descricao não encontrada em nivel3_validas).
    if not plano_df.empty:
        validar_categorias_dre(
            lancamentos=resultado.lancamentos,
            nivel3_validas=nivel3_validas,
            mapeamentos_supabase=_mapeamentos_supabase,
        )

    # ── Escrita dos CSVs ──────────────────────────────────────────────────
    print("\nGerando arquivos de saída:")
    _salvar_csv(
        resultado.lancamentos,
        output_path / "lancamentos_normalizados.csv",
        "Lançamentos normalizados",
    )
    _salvar_csv(
        resultado.inconsistencias,
        output_path / "inconsistencias.csv",
        "Inconsistências",
    )
    _salvar_csv(
        resultado.categorias_nao_mapeadas,
        output_path / "categorias_nao_mapeadas.csv",
        "Categorias não mapeadas",
    )

    # ── Resumo de conferência ─────────────────────────────────────────────
    resumo = _gerar_resumo(df, resultado, config)
    _salvar_csv(resumo, output_path / "resumo_conferencia.csv", "Resumo de conferência")

    # ── Resumo de auditoria (Zeus multi-CNPJ) ────────────────────────────
    auditoria = resultado.metadata.get("auditoria", [])
    if auditoria:
        _salvar_csv(auditoria, output_path / "resumo_auditoria.csv", "Resumo de auditoria")
        print("\n  Auditoria por empresa / tipo:")
        for row in auditoria:
            emp  = row["empresa_origem"]
            tipo = row.get("tipo") or ""
            qtd  = row["qtd_lancamentos"]
            tot  = row["total"]
            print(f"    {emp:<20} {tipo:<8} {qtd:>5} lançamentos   R$ {tot:>14,.2f}")

    # ── DRE ───────────────────────────────────────────────────────────────
    # Usa plano_dre (somente linhas exibir_dre=True) para não incluir contas
    # de auditoria/conferência no demonstrativo.
    dre_rows = []
    if not plano_dre.empty:
        dre_rows = gerar_dre(resultado.lancamentos, plano_dre)
        _salvar_csv(dre_rows, output_path / "dre_mensal.csv", "DRE mensal")

    # ── Supabase (opcional) ───────────────────────────────────────────────
    if save_supabase:
        try:
            from src.supabase_client import salvar_no_supabase

            # ── Checkpoint 4: nome exato que será passado ao Supabase ─────
            _nome_para_supabase = _nome_config   # ex.: "Zeus"
            print(f"\nCLIENTE RECEBIDO NO SUPABASE SAVE: {_nome_para_supabase!r}")
            logger.info("Pipeline → Supabase: nome_cliente=%r | mes_ref=%s", _nome_para_supabase, mes_ref)

            print("Salvando no Supabase...")
            salvar_no_supabase(
                _nome_para_supabase,    # ← variável local, não config["nome"] direto
                input_file,
                resultado.lancamentos,
                resultado.inconsistencias,
                resumo[0] if resumo else {},
                plano_df,
                dre_rows,
                mes_ref=mes_ref,
                overwrite=overwrite,
            )
            print("  Supabase: dados salvos com sucesso.")
        except RuntimeError as exc:
            # RuntimeError = erro de segurança (IDs cruzados, divergência de dados).
            # NÃO engolir — abortar o processo para evitar corrupção de dados.
            logger.error("Supabase ERRO CRÍTICO: %s", exc, exc_info=True)
            sys.exit(f"\n  ✖  ERRO CRÍTICO Supabase (abortando): {exc}")
        except Exception as exc:
            print(f"\n  ⚠  Erro ao salvar no Supabase: {exc}")
            logger.error("Erro Supabase: %s", exc, exc_info=True)

    if resultado.inconsistencias:
        print(
            f"\n  ⚠  {len(resultado.inconsistencias)} inconsistência(s) encontrada(s). "
            "Verifique inconsistencias.csv."
        )
    print("\nETL concluído.\n")
