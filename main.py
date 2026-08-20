"""
ETL Financeiro — Conta Azul / Wingraph

════════════════════════════════════════════════════════════
Estrutura canônica do projeto:

  cfg/clientes/{cliente}/
      config.yml                ← PDFs por mês (Zeus) e futuros configs
      plano_contas_cliente.csv  ← plano de contas

  data/raw/{cliente}/{mes_ref}/ ← arquivos brutos de entrada (PDF, XLS)
  saida/{cliente}/{mes_ref}/    ← CSVs gerados pelo ETL

════════════════════════════════════════════════════════════
Uso (clientes Excel, ex.: Zixbe):
    python main.py data/raw/zixbe/2026-04/relatorio.xlsx --client zixbe --mes-ref 2026-04

Uso (clientes PDF, ex.: Zeus — sem arquivo de entrada):
    python main.py --client zeus --mes-ref 2026-01

Flags comuns:
    --output-dir DIR        Diretório raiz de saída (padrão: ./saida)
                            Os CSVs ficam em {DIR}/{cliente}/{mes-ref}/
    --save-supabase         Salva no Supabase (requer .env com as chaves)
    --overwrite             Sobrescreve dados existentes no Supabase

Caminhos legados são aceitos com aviso — mova os arquivos para a estrutura
canônica para eliminar os avisos.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
print(".env carregado")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL financeiro para arquivos Excel do Conta Azul.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default="",
        help="Caminho para o arquivo Excel (.xlsx / .xls). Opcional para clientes PDF (ex.: Zeus).",
    )
    parser.add_argument(
        "--client",
        default="zixbe",
        metavar="NOME",
        help="Nome do cliente (padrão: zixbe)",
    )
    parser.add_argument(
        "--output-dir",
        default="./saida",
        metavar="DIR",
        help="Diretório de saída dos CSVs (padrão: ./saida)",
    )
    parser.add_argument(
        "--save-supabase",
        action="store_true",
        help="Salva os resultados no Supabase (requer SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY)",
    )
    parser.add_argument(
        "--mes-ref",
        metavar="AAAA-MM",
        help="Mês de referência (obrigatório com --save-supabase). Exemplo: 2026-04",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Sobrescreve dados existentes no Supabase para o cliente/mês informado",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # ── Checkpoint 1: valor exato que saiu do argparse ────────────────────
    _cliente_main = args.client
    print(f"CLIENTE RECEBIDO NO MAIN: {_cliente_main!r}")

    # Valida arquivo de entrada somente se foi informado
    if args.input_file:
        filepath = Path(args.input_file)
        if not filepath.exists():
            sys.exit(f"Arquivo não encontrado: {filepath}")
        if filepath.suffix.lower() not in {".xlsx", ".xls"}:
            sys.exit(f"Formato não suportado: '{filepath.suffix}'. Use .xlsx ou .xls.")

    if args.save_supabase and not args.mes_ref:
        sys.exit(
            "Erro: --mes-ref é obrigatório quando --save-supabase é usado.\n"
            "Exemplo: --mes-ref 2026-04"
        )

    from etl.pipeline import run_pipeline
    run_pipeline(
        args.input_file,
        _cliente_main,          # usa variável local, não args.client
        args.output_dir,
        save_supabase=args.save_supabase,
        mes_ref=args.mes_ref,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
