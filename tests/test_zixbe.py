"""
Testes unitários do processador Zixbe.
Execute com:  python -m pytest tests/ -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CLIENTES
from etl.clients.zixbe import (
    ZixbeProcessor,
    parse_brl_number,
    _build_category_blocks,
)


CONFIG = CLIENTES["zixbe"]

# Nomes de coluna reais do Conta Azul (usados pelo config de produção)
_COL_DATA       = "Data de competência"
_COL_DESC       = "Descrição"
_COL_FORN       = "Nome do fornecedor/cliente"
_COL_VALOR      = "Valor (R$)"


def _linha(**kwargs) -> pd.Series:
    """Cria uma pd.Series simulando uma linha do relatório Conta Azul."""
    base = {
        _COL_DATA:  pd.Timestamp("2024-03-01"),
        _COL_DESC:  "Teste",
        _COL_FORN:  "Fornecedor X",
        _COL_VALOR: -100.0,
        "Categoria 1":          "Serviços",
        "Valor na Categoria 1": -100.0,
    }
    base.update(kwargs)
    return pd.Series(base)


def _df(*linhas) -> pd.DataFrame:
    return pd.DataFrame(list(linhas))


# ---------------------------------------------------------------------------
# parse_brl_number
# ---------------------------------------------------------------------------

class TestParseBrlNumber:
    def test_milhar_com_virgula_decimal(self):
        assert parse_brl_number("15.000,00") == 15000.0

    def test_negativo_sem_milhar(self):
        assert parse_brl_number("-922,50") == -922.5

    def test_milhar_negativo(self):
        assert parse_brl_number("-14.077,50") == -14077.5

    def test_float_ja_numerico(self):
        assert parse_brl_number(15000.0) == 15000.0

    def test_int_numerico(self):
        assert parse_brl_number(550) == 550.0

    def test_string_sem_milhar(self):
        assert parse_brl_number("922.50") == 922.5

    def test_vazio_retorna_none(self):
        assert parse_brl_number("") is None
        assert parse_brl_number(None) is None
        assert parse_brl_number(float("nan")) is None

    def test_preserva_negativo_numerico(self):
        assert parse_brl_number(-922.5) == -922.5


# ---------------------------------------------------------------------------
# _build_category_blocks
# ---------------------------------------------------------------------------

class TestBuildCategoryBlocks:
    def test_bloco_simples(self):
        cols = [
            "Categoria 1", "Valor na Categoria 1",
            "Centro de Custo 1", "Valor no Centro de Custo 1",
        ]
        blocks = _build_category_blocks(cols, CONFIG["prefixos"])
        assert len(blocks) == 1
        assert blocks[0]["cat_col"] == "Categoria 1"
        assert blocks[0]["val_cat_col"] == "Valor na Categoria 1"
        assert blocks[0]["cc_pairs"] == [("Centro de Custo 1", "Valor no Centro de Custo 1")]

    def test_dois_blocos_com_cc_duplicado(self):
        # Estrutura real do Conta Azul com Categoria 2 + CC renomeado pelo pandas
        cols = [
            "Categoria 1", "Valor na Categoria 1",
            "Centro de Custo 1", "Valor no Centro de Custo 1",
            "Centro de Custo 2", "Valor no Centro de Custo 2",
            "Categoria 2", "Valor na Categoria 2",
            "Centro de Custo 1.1", "Valor no Centro de Custo 1.1",
        ]
        blocks = _build_category_blocks(cols, CONFIG["prefixos"])
        assert len(blocks) == 2

        b0 = blocks[0]
        assert b0["cat_col"] == "Categoria 1"
        cc_names_b0 = [p[0] for p in b0["cc_pairs"]]
        assert "Centro de Custo 1" in cc_names_b0
        assert "Centro de Custo 2" in cc_names_b0
        assert "Centro de Custo 1.1" not in cc_names_b0

        b1 = blocks[1]
        assert b1["cat_col"] == "Categoria 2"
        cc_names_b1 = [p[0] for p in b1["cc_pairs"]]
        assert cc_names_b1 == ["Centro de Custo 1.1"]

    def test_sem_cc(self):
        cols = ["Categoria 1", "Valor na Categoria 1"]
        blocks = _build_category_blocks(cols, CONFIG["prefixos"])
        assert blocks[0]["cc_pairs"] == []


# ---------------------------------------------------------------------------
# Expansão por bloco — cenários concretos
# ---------------------------------------------------------------------------

class TestExpansao:
    def setup_method(self):
        self.proc = ZixbeProcessor(CONFIG)

    def test_linha_simples(self):
        df = _df(_linha())
        r = self.proc.processar(df)
        assert len(r.lancamentos) == 1
        assert r.lancamentos[0]["valor"] == -100.0
        assert r.lancamentos[0]["centro_custo"] is None
        assert r.lancamentos[0]["status_validacao"] == "ok"

    def test_multiplas_categorias_sem_cc(self):
        linha = _linha(**{
            "Categoria 1": "Software", "Valor na Categoria 1": -60.0,
            "Categoria 2": "Hardware", "Valor na Categoria 2": -40.0,
            _COL_VALOR: -100.0,
        })
        r = self.proc.processar(_df(linha))
        assert len(r.lancamentos) == 2
        cats = [l["categoria_origem"] for l in r.lancamentos]
        assert cats == ["Software", "Hardware"]
        assert all(l["status_validacao"] == "ok" for l in r.lancamentos)

    def test_venda_com_imposto_retido_duas_categorias_cc_proprio(self):
        """
        Cenário real: Categoria 1 (receita) + Categoria 2 (imposto retido),
        cada uma com seu próprio CC.  Soma deve fechar com Valor (R$).
        """
        linha = _linha(**{
            _COL_VALOR: 14077.5,
            "Categoria 1":            "Consultoria de Negócios",
            "Valor na Categoria 1":   15000.0,
            "Centro de Custo 1":      "SICOOB CREDIVALE",
            "Valor no Centro de Custo 1": 15000.0,
            "Categoria 2":            "Impostos retidos em vendas",
            "Valor na Categoria 2":   -922.5,
            "Centro de Custo 1.1":    "SICOOB CREDIVALE",
            "Valor no Centro de Custo 1.1": -922.5,
        })
        r = self.proc.processar(_df(linha))

        assert len(r.lancamentos) == 2
        assert len(r.inconsistencias) == 0

        l0, l1 = r.lancamentos
        assert l0["categoria_origem"] == "Consultoria de Negócios"
        assert l0["valor"] == 15000.0
        assert l0["centro_custo"] == "SICOOB CREDIVALE"

        assert l1["categoria_origem"] == "Impostos retidos em vendas"
        assert l1["valor"] == -922.5
        assert l1["centro_custo"] == "SICOOB CREDIVALE"

    def test_aluguel_rateado_3_cc_mesma_categoria(self):
        """Categoria única rateada em 3 centros de custo → 3 linhas com a mesma categoria."""
        linha = _linha(**{
            _COL_VALOR: -550.0,
            "Categoria 1":                     "Aluguel e Condomínio",
            "Valor na Categoria 1":            -550.0,
            "Centro de Custo 1":               "E3 EDUCAÇÃO",
            "Valor no Centro de Custo 1":      -183.26,
            "Centro de Custo 2":               "ZIXBE",
            "Valor no Centro de Custo 2":      -183.37,
            "Centro de Custo 3":               "FEELBIX",
            "Valor no Centro de Custo 3":      -183.37,
        })
        r = self.proc.processar(_df(linha))

        assert len(r.lancamentos) == 3
        categorias = {l["categoria_origem"] for l in r.lancamentos}
        assert categorias == {"Aluguel e Condomínio"}

        centros = {l["centro_custo"] for l in r.lancamentos}
        assert centros == {"E3 EDUCAÇÃO", "ZIXBE", "FEELBIX"}

        assert len(r.inconsistencias) == 0

    def test_cc_da_cat2_nao_contamina_cat1(self):
        """CC do bloco 1 (Categoria 2) não aparece nas linhas da Categoria 1."""
        linha = _linha(**{
            _COL_VALOR: -100.0,
            "Categoria 1":            "Serviços",
            "Valor na Categoria 1":   -80.0,
            "Centro de Custo 1":      "TI",
            "Valor no Centro de Custo 1": -80.0,
            "Categoria 2":            "Impostos",
            "Valor na Categoria 2":   -20.0,
            "Centro de Custo 1.1":    "Fiscal",
            "Valor no Centro de Custo 1.1": -20.0,
        })
        r = self.proc.processar(_df(linha))

        assert len(r.lancamentos) == 2
        l0 = next(l for l in r.lancamentos if l["categoria_origem"] == "Serviços")
        l1 = next(l for l in r.lancamentos if l["categoria_origem"] == "Impostos")

        assert l0["centro_custo"] == "TI"
        assert l1["centro_custo"] == "Fiscal"

    def test_valores_negativos_mantidos(self):
        df = _df(_linha(**{_COL_VALOR: -250.0, "Valor na Categoria 1": -250.0}))
        r = self.proc.processar(df)
        assert r.lancamentos[0]["valor"] == -250.0

    def test_valor_positivo_mantido(self):
        df = _df(_linha(**{
            _COL_VALOR: 5000.0,
            "Categoria 1": "Receita",
            "Valor na Categoria 1": 5000.0,
        }))
        r = self.proc.processar(df)
        assert r.lancamentos[0]["valor"] == 5000.0


# ---------------------------------------------------------------------------
# Validação de soma
# ---------------------------------------------------------------------------

class TestValidacao:
    def setup_method(self):
        self.proc = ZixbeProcessor(CONFIG)

    def test_divergencia_registrada(self):
        linha = _linha(**{
            "Categoria 1": "A", "Valor na Categoria 1": -80.0,
            "Categoria 2": "B", "Valor na Categoria 2": -10.0,
            _COL_VALOR: -100.0,  # soma -90, divergência de -10
        })
        r = self.proc.processar(_df(linha))
        assert len(r.inconsistencias) == 1
        assert all(l["status_validacao"] == "divergencia" for l in r.lancamentos)

    def test_sem_divergencia(self):
        linha = _linha(**{
            "Categoria 1": "A", "Valor na Categoria 1": -60.0,
            "Categoria 2": "B", "Valor na Categoria 2": -40.0,
            _COL_VALOR: -100.0,
        })
        r = self.proc.processar(_df(linha))
        assert len(r.inconsistencias) == 0

    def test_tolerancia_centavo(self):
        linha = _linha(**{"Valor na Categoria 1": -99.995, _COL_VALOR: -100.0})
        r = self.proc.processar(_df(linha))
        assert len(r.inconsistencias) == 0

    def test_venda_com_imposto_soma_fecha(self):
        """15.000,00 + (-922,50) = 14.077,50 — sem inconsistência."""
        linha = _linha(**{
            _COL_VALOR: 14077.50,
            "Categoria 1":            "Consultoria de Negócios",
            "Valor na Categoria 1":   15000.0,
            "Centro de Custo 1":      "SICOOB CREDIVALE",
            "Valor no Centro de Custo 1": 15000.0,
            "Categoria 2":            "Impostos retidos em vendas",
            "Valor na Categoria 2":   -922.5,
            "Centro de Custo 1.1":    "SICOOB CREDIVALE",
            "Valor no Centro de Custo 1.1": -922.5,
        })
        r = self.proc.processar(_df(linha))
        assert len(r.inconsistencias) == 0
        soma = sum(l["valor"] for l in r.lancamentos)
        assert abs(soma - 14077.50) < 0.01


# ---------------------------------------------------------------------------
# Campos auxiliares
# ---------------------------------------------------------------------------

class TestCamposAuxiliares:
    def setup_method(self):
        self.proc = ZixbeProcessor(CONFIG)

    def test_id_linha_origem(self):
        df = _df(_linha(), _linha())
        r = self.proc.processar(df)
        ids = [l["id_linha_origem"] for l in r.lancamentos]
        assert ids == [1, 2]

    def test_mes_referencia(self):
        df = _df(_linha(**{_COL_DATA: pd.Timestamp("2024-07-15")}))
        r = self.proc.processar(df)
        assert r.lancamentos[0]["mes_referencia"] == "2024-07"

    def test_nome_cliente(self):
        df = _df(_linha())
        r = self.proc.processar(df)
        assert r.lancamentos[0]["cliente"] == "Zixbe"


# ---------------------------------------------------------------------------
# Categorias não mapeadas
# ---------------------------------------------------------------------------

class TestCategoriasNaoMapeadas:
    def setup_method(self):
        self.proc = ZixbeProcessor({**CONFIG, "mapeamento_categorias": {}})

    def test_captura_categorias_unicas(self):
        df = _df(
            _linha(**{"Categoria 1": "Serviços Terceiros"}),
            _linha(**{"Categoria 1": "Infraestrutura"}),
            _linha(**{"Categoria 1": "Serviços Terceiros"}),  # duplicata
        )
        r = self.proc.processar(df)
        cats = {c["categoria_original"] for c in r.categorias_nao_mapeadas}
        assert cats == {"Serviços Terceiros", "Infraestrutura"}

    def test_mapeamento_remove_da_lista(self):
        proc = ZixbeProcessor({**CONFIG, "mapeamento_categorias": {"Serviços": "Serviços Padrão"}})
        df = _df(_linha(**{"Categoria 1": "Serviços"}))
        r = proc.processar(df)
        assert len(r.categorias_nao_mapeadas) == 0
