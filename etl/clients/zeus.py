"""
Processador Zeus — lê múltiplos PDFs Wingraph (AP + AR, multi-CNPJ)
configurados em cfg/clientes/zeus/config.yml e produz ResultadoETL consolidado.

Cada entrada do YAML define:
  empresa_origem: nome da empresa/CNPJ (auditoria)
  tipo:           pagar | receber
  arquivo:        caminho absoluto ou relativo à raiz do projeto

Todos os PDFs do mesmo mês são consolidados em um único ResultadoETL.
empresa_origem é propagada nos lançamentos apenas para auditoria/filtro;
não afeta o DRE nem vira centro de custo.

Estrutura canônica:
  cfg/clientes/zeus/config.yml   ← mapeamento de PDFs por mês
  data/raw/zeus/{mes_ref}/       ← PDFs originais (recomendado)
"""

import logging
from collections import defaultdict
from pathlib import Path
from typing import List

import pandas as pd

from etl.clients.base import ProcessadorBase, ResultadoETL
from etl.clients.zeus_reader import parse_wingraph_pdf
from etl.paths import resolver_config_yaml

logger = logging.getLogger(__name__)

# Raiz do projeto (dois níveis acima deste arquivo)
_RAIZ = Path(__file__).resolve().parent.parent.parent

# Caminho legado anterior ao refactor (mantido para fallback com aviso)
_YAML_LEGADO = "cfg/zeus.yml"


def _carregar_yaml(yaml_path: str) -> dict:
    """
    Carrega o YAML de configuração do Zeus.
    Usa resolver_config_yaml para suportar caminho canônico e legado.
    """
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML não instalado. Execute: pip install pyyaml")

    # Tenta resolver via paths (canônico → legado)
    caminho_resolvido = resolver_config_yaml("zeus", legacy_path=_YAML_LEGADO)
    if caminho_resolvido:
        path = Path(caminho_resolvido)
    else:
        # Fallback: usa yaml_path passado diretamente pelo config
        path = Path(yaml_path)
        if not path.is_absolute():
            path = _RAIZ / path

    if not path.exists():
        raise FileNotFoundError(
            f"Config YAML do Zeus não encontrado.\n"
            f"  Esperado: {_RAIZ / 'cfg' / 'clientes' / 'zeus' / 'config.yml'}\n"
            f"  Tentado : {path}\n"
            "  Crie o arquivo ou ajuste config_yaml_path em config.py."
        )

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolver_arquivo(arquivo: str) -> str:
    """Resolve caminhos relativos em relação à raiz do projeto."""
    p = Path(arquivo)
    if p.is_absolute():
        return str(p)
    return str(_RAIZ / p)


class ZeusProcessor(ProcessadorBase):
    """
    Processador para relatórios Wingraph do cliente Zeus.

    config esperado:
        nome              (str)  "Zeus"
        tipo_fonte        (str)  "pdf"
        config_yaml_path  (str)  caminho para config/zeus.yml
        mes_ref           (str)  mês a processar, ex: "2026-01" (injetado pelo pipeline)
        mapeamento_categorias (dict)  sobrescritas manuais
    """

    def __init__(self, config: dict):
        self.config = config

    def processar(self, df: pd.DataFrame) -> ResultadoETL:
        # df ignorado — a leitura vem dos PDFs configurados no YAML
        nome_cliente = self.config.get("nome", "Zeus")
        mes_ref      = self.config.get("mes_ref")
        yaml_path    = self.config.get("config_yaml_path", "config/zeus.yml")
        mapeamento   = self.config.get("mapeamento_categorias", {})

        if not mes_ref:
            raise ValueError("Zeus: 'mes_ref' não informado. Use --mes-ref AAAA-MM.")

        # Carrega YAML e localiza entradas do mês
        dados_yaml = _carregar_yaml(yaml_path)
        pdf_entries: List[dict] = dados_yaml.get(mes_ref, [])
        if not pdf_entries:
            raise ValueError(
                f"Zeus: nenhum PDF configurado para '{mes_ref}' em {yaml_path}.\n"
                f"  Meses disponíveis: {list(dados_yaml.keys())}"
            )

        resultado = ResultadoETL()

        # Auditoria: acumula stats por empresa_origem + tipo
        stats: dict = defaultdict(lambda: {"qtd": 0, "total": 0.0})
        id_base = 0

        print(f"\n  PDFs configurados para {mes_ref}: {len(pdf_entries)}")

        for entrada in pdf_entries:
            empresa   = entrada.get("empresa_origem", nome_cliente)
            tipo      = entrada.get("tipo", "").lower()
            arquivo   = _resolver_arquivo(entrada.get("arquivo", ""))

            print(f"    [{tipo:7}] {empresa} → {Path(arquivo).name}")

            if not Path(arquivo).exists():
                print(f"    ⚠  Arquivo não encontrado: {arquivo}")
                logger.warning("Zeus: arquivo não encontrado: %s", arquivo)
                continue

            lancamentos_brutos = parse_wingraph_pdf(arquivo, nome_cliente, empresa_origem=empresa)

            for lanc in lancamentos_brutos:
                id_base += 1
                cat_orig   = lanc.get("categoria_origem") or ""
                cat_mapped = mapeamento.get(cat_orig, cat_orig)

                rec = {
                    "cliente":            lanc["cliente"],
                    "empresa_origem":     lanc["empresa_origem"],
                    "mes_referencia":     mes_ref,          # usa o parâmetro CLI, não o do PDF
                    "tipo_lancamento":    lanc["tipo_lancamento"],
                    "data_competencia":   lanc["data_competencia"],
                    "numero_titulo":      lanc["numero_titulo"],
                    "fornecedor_cliente": lanc["fornecedor_cliente"],
                    "descricao":          lanc["descricao"],
                    "categoria_origem":   cat_mapped,
                    "valor":              lanc["valor"],
                    "saldo":              lanc["saldo"],
                    "id_linha_origem":    id_base,
                    "status_validacao":   lanc["status_validacao"],
                    "observacao_etl":     lanc["observacao_etl"],
                }
                resultado.lancamentos.append(rec)

                if lanc["status_validacao"] != "ok":
                    resultado.inconsistencias.append({
                        "id_linha_origem":    id_base,
                        "empresa_origem":     empresa,
                        "data_competencia":   lanc["data_competencia"],
                        "fornecedor_cliente": lanc["fornecedor_cliente"],
                        "categoria_origem":   cat_mapped,
                        "valor":              lanc["valor"],
                        "observacao":         lanc["observacao_etl"],
                    })

                chave = (empresa, lanc["tipo_lancamento"])
                stats[chave]["qtd"]   += 1
                stats[chave]["total"] += lanc["valor"]

            # Categorias sem mapeamento manual
            cats_no_pdf = {l.get("categoria_origem") for l in lancamentos_brutos}
            for cat in sorted(cats_no_pdf):
                if cat and cat not in mapeamento:
                    resultado.categorias_nao_mapeadas.append({
                        "categoria_original": cat,
                        "categoria_mapeada":  cat,
                        "situacao":           "auto",
                    })

        # Remove duplicatas em categorias_nao_mapeadas
        vistas = set()
        unicas = []
        for r in resultado.categorias_nao_mapeadas:
            k = r["categoria_original"]
            if k not in vistas:
                vistas.add(k)
                unicas.append(r)
        resultado.categorias_nao_mapeadas = unicas

        # ── Auditoria ────────────────────────────────────────────────────────
        auditoria_rows = []
        total_consolidado = 0.0
        for (empresa, tipo), s in sorted(stats.items()):
            auditoria_rows.append({
                "empresa_origem": empresa,
                "tipo":           tipo,
                "qtd_lancamentos": s["qtd"],
                "total":          round(s["total"], 2),
            })
            total_consolidado += s["total"]

        auditoria_rows.append({
            "empresa_origem": "CONSOLIDADO",
            "tipo":           "",
            "qtd_lancamentos": len(resultado.lancamentos),
            "total":          round(total_consolidado, 2),
        })

        resultado.metadata = {
            "qtd_pdfs":     len(pdf_entries),
            "auditoria":    auditoria_rows,
        }

        logger.info(
            "Zeus: %d lançamento(s) de %d PDF(s) | %d inconsistência(s).",
            len(resultado.lancamentos), len(pdf_entries), len(resultado.inconsistencias),
        )
        return resultado
