"""
Testes da infraestrutura declarativa de contas calculadas (tipo_conta/formula).

Cobrem:
  - regressão: os 3 planos reais (fixtures) continuam idênticos sem declarar
    as colunas novas (o fallback legado _FORMULAS_RESULTADO continua ativo);
  - declarativo: conta calculada simples, soma, soma+subtração, dependência
    entre calculadas, referência em nível 2/3/4, rename de descricao sem
    quebrar (referência é por id_conta), ordem física das linhas irrelevante;
  - erros: id inexistente, fórmula vazia, sintaxe inválida, autorreferência,
    ciclo, conta calculada com categoria_origem, conta calculada agregadora.

Semântica documentada (ver também docstring de etl/dre.py):
  Uma conta calculada opera sobre o valor_base NATURAL (pré-sinal) de suas
  referências, e seu próprio `sinal` é aplicado depois, uniformemente com
  folha/agregadora. Isso é DIFERENTE do fallback legado (_FORMULAS_RESULTADO),
  que opera sobre valores JÁ EXIBIDOS (pós-sinal) — preservado assim de
  propósito para não alterar nenhum resultado de plano existente. Por isso,
  o coeficiente de uma conta com sinal=-1 na fórmula nova tem o sinal
  aritmético "invertido" em relação ao que se esperaria olhando só para o
  dict legado — ver TestSemanticaSinalNaturalVsExibido abaixo.

Execute com: python -m pytest tests/test_formulas_calculadas.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etl.plano_contas import ler_plano_contas_df, parse_formula
from etl.dre import gerar_dre

FIXTURES = Path(__file__).parent / "fixtures"


def _mapa(rows):
    return {r["descricao"]: r["valor"] for r in rows}


def _escrever_plano(tmp_path, nome, linhas_dados, header=None):
    header = header or (
        "id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;"
        "auditoria_only;categoria_origem;tipo_conta;formula"
    )
    conteudo = header + "\n" + "\n".join(f'"{linha}"' for linha in linhas_dados) + "\n"
    p = tmp_path / nome
    p.write_text(conteudo, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Regressão — planos reais (rodada de hierarquia variável) continuam iguais
# sem declarar tipo_conta/formula.
# ---------------------------------------------------------------------------

class TestRegressaoSemColunasNovas:
    @pytest.mark.parametrize("arquivo", [
        "plano_contas_zixbe.csv",
        "plano_contas_zeus_legado.csv",
        "plano_contas_zeus_novo.csv",
    ])
    def test_tipo_conta_default_estrutural_para_todas_as_linhas(self, arquivo):
        df = ler_plano_contas_df(str(FIXTURES / arquivo))
        assert not df.empty
        assert (df["tipo_conta"] == "estrutural").all()
        assert df["formula"].isna().all()

    def test_zixbe_receita_bruta_identica_ao_fallback_legado(self):
        plano = ler_plano_contas_df(str(FIXTURES / "plano_contas_zixbe.csv"))
        lancs = [{"categoria_origem": "Consultoria de Negócios", "valor": 25.0, "mes_referencia": "2026-01"}]
        rows = gerar_dre(lancs, plano)
        by_desc = _mapa(rows)
        # RECEITA BRUTA (id=1, agregador estrutural, nao calculada) = 25;
        # RECEITA LÍQUIDA (fallback legado, nao declarada) = 25 - 0 = 25.
        assert by_desc["RECEITA BRUTA"] == 25.0
        assert by_desc["RECEITA LÍQUIDA"] == 25.0


# ---------------------------------------------------------------------------
# Declarativo
# ---------------------------------------------------------------------------

_PLANO_BASICO = [
    "1;1;;10;Conta A;1;Sim;Não;Categoria A;;",
    "2;1;;20;Conta B;1;Sim;Não;Categoria B;;",
    "3;1;;30;Calc Simples;1;Sim;Não;;calculada;+1*1",
    "4;1;;40;Calc Soma;1;Sim;Não;;calculada;+1*1 +1*2",
    "5;1;;50;Calc Subtracao;1;Sim;Não;;calculada;+1*1 -1*2",
    "6;1;;60;Calc Dependente;1;Sim;Não;;calculada;+1*5 +1*2",
]

_LANCAMENTOS_BASICO = [
    # categoria_origem aqui é a descricao FINAL (pós-mapeamento) da folha —
    # gerar_dre() casa lançamentos por descricao, não pela coluna
    # categoria_origem do CSV (essa é só usada por ler_mapeamento_plano()
    # para construir o de/para ERP → descricao, um passo anterior a este).
    {"categoria_origem": "Conta A", "valor": 100.0, "mes_referencia": "2026-01"},
    {"categoria_origem": "Conta B", "valor": 30.0, "mes_referencia": "2026-01"},
]


class TestContaCalculadaBasico:
    def test_calc_simples_soma_e_subtracao_e_dependencia(self, tmp_path):
        path = _escrever_plano(tmp_path, "plano.csv", _PLANO_BASICO)
        plano = ler_plano_contas_df(path)
        rows = gerar_dre(_LANCAMENTOS_BASICO, plano)
        by_desc = _mapa(rows)

        assert by_desc["Conta A"] == 100.0
        assert by_desc["Conta B"] == 30.0
        assert by_desc["Calc Simples"] == 100.0          # +1*A
        assert by_desc["Calc Soma"] == 130.0              # +1*A +1*B
        assert by_desc["Calc Subtracao"] == 70.0          # +1*A -1*B
        # Calc Dependente = +1*(Calc Subtracao) +1*B = 70 + 30 = 100
        # -- depende do valor_base de OUTRA conta calculada, resolvido por
        # recursão, não pela ordem física das linhas do CSV.
        assert by_desc["Calc Dependente"] == 100.0

    def test_referencia_e_por_id_conta_sobrevive_a_rename_de_descricao(self, tmp_path):
        """Renomear a descricao da conta referenciada (id=2) não pode quebrar
        a fórmula nem mudar nenhum resultado — a referência é por id_conta."""
        path_original = _escrever_plano(tmp_path, "original.csv", _PLANO_BASICO)
        plano_original = ler_plano_contas_df(path_original)
        rows_original = gerar_dre(_LANCAMENTOS_BASICO, plano_original)

        linhas_renomeadas = [
            l.replace("Conta B;1;Sim;Não;Categoria B", "Conta B RENOMEADA;1;Sim;Não;Categoria B")
            for l in _PLANO_BASICO
        ]
        # O lançamento upstream já chega com a descricao ATUAL (é assim que o
        # mapeamento real funciona — categoria_origem do lançamento = descricao
        # de destino corrente). Só a descricao mudou; o id_conta referenciado
        # pela fórmula ("+1*1 +1*2", id=2) continua o mesmo.
        lancamentos_renomeado = [
            _LANCAMENTOS_BASICO[0],
            {**_LANCAMENTOS_BASICO[1], "categoria_origem": "Conta B RENOMEADA"},
        ]
        path_renomeado = _escrever_plano(tmp_path, "renomeado.csv", linhas_renomeadas)
        plano_renomeado = ler_plano_contas_df(path_renomeado)
        rows_renomeado = gerar_dre(lancamentos_renomeado, plano_renomeado)

        valores_original = {r["ordem"]: r["valor"] for r in rows_original}
        valores_renomeado = {r["ordem"]: r["valor"] for r in rows_renomeado}
        assert valores_original == valores_renomeado
        assert _mapa(rows_renomeado)["Conta B RENOMEADA"] == 30.0
        assert _mapa(rows_renomeado)["Calc Soma"] == 130.0  # formula "+1*1 +1*2" nao mudou

    def test_ordem_fisica_das_linhas_nao_afeta_resultado(self, tmp_path):
        path_original = _escrever_plano(tmp_path, "original.csv", _PLANO_BASICO)
        rows_original = gerar_dre(_LANCAMENTOS_BASICO, ler_plano_contas_df(path_original))

        embaralhado = list(reversed(_PLANO_BASICO))  # ordem fisica invertida
        path_embaralhado = _escrever_plano(tmp_path, "embaralhado.csv", embaralhado)
        rows_embaralhado = gerar_dre(_LANCAMENTOS_BASICO, ler_plano_contas_df(path_embaralhado))

        assert _mapa(rows_original) == _mapa(rows_embaralhado)


class TestReferenciaEmProfundidadeVariavel:
    _PLANO_PROFUNDO = [
        "1;1;;10;Grupo Nivel1;1;Sim;Não;;;",
        "2;2;1;20;Grupo Nivel2;1;Sim;Não;;;",
        "3;3;2;30;Grupo Nivel3;1;Sim;Não;;;",
        "4;4;3;40;Folha Nivel4;1;Sim;Não;Categoria Profunda;;",
        "5;1;;50;Calc Referencia Profunda;1;Sim;Não;;calculada;+1*4",
    ]

    def test_formula_referencia_folha_de_nivel_4_direto(self, tmp_path):
        path = _escrever_plano(tmp_path, "profundo.csv", self._PLANO_PROFUNDO)
        plano = ler_plano_contas_df(path)
        lancs = [{"categoria_origem": "Folha Nivel4", "valor": 55.0, "mes_referencia": "2026-01"}]
        rows = gerar_dre(lancs, plano)
        by_desc = _mapa(rows)

        assert by_desc["Folha Nivel4"] == 55.0
        assert by_desc["Grupo Nivel3"] == 55.0   # rollup normal ate a raiz
        assert by_desc["Grupo Nivel2"] == 55.0
        assert by_desc["Grupo Nivel1"] == 55.0
        # referencia direta ao id=4 (nivel 4), sem passar pelo rollup —
        # mesmo valor, por caminho diferente.
        assert by_desc["Calc Referencia Profunda"] == 55.0


class TestSemanticaSinalNaturalVsExibido:
    """
    Documenta explicitamente a semântica: a fórmula nova opera sobre
    valor_base NATURAL (pré-sinal) das referências, não sobre o valor já
    exibido. Por isso o coeficiente de uma conta com sinal=-1 é "+1" aqui,
    não "-1" como seria no dict legado (_FORMULAS_RESULTADO), que opera
    sobre valores JÁ EXIBIDOS (pós-sinal).
    """
    _PLANO = [
        "1;1;;10;Receita;1;Sim;Não;Categoria Receita;;",
        "2;1;;20;Deducao;-1;Sim;Não;Categoria Deducao;;",
        # Deducao natural = -200 (sinal natural de despesa/deducao).
        # Exibido = (-200) * sinal(-1) = 200 (magnitude positiva).
        "3;1;;30;Receita Liquida Nova;1;Sim;Não;;calculada;+1*1 +1*2",
        # coeficiente +1 (nao -1!) porque opera no valor_base NATURAL de
        # Deducao, que ja e negativo -- somar o natural negativo produz o
        # mesmo efeito de "subtrair a deducao exibida".
    ]

    def test_coeficiente_positivo_sobre_valor_natural_subtrai_a_deducao_exibida(self, tmp_path):
        path = _escrever_plano(tmp_path, "sinal.csv", self._PLANO)
        plano = ler_plano_contas_df(path)
        lancs = [
            {"categoria_origem": "Receita", "valor": 1000.0, "mes_referencia": "2026-01"},
            {"categoria_origem": "Deducao", "valor": -200.0, "mes_referencia": "2026-01"},
        ]
        rows = gerar_dre(lancs, plano)
        by_desc = _mapa(rows)

        assert by_desc["Receita"] == 1000.0
        assert by_desc["Deducao"] == 200.0          # exibido: magnitude positiva
        assert by_desc["Receita Liquida Nova"] == 800.0  # 1000 - 200, via +1*1 +1*2 em natural


# ---------------------------------------------------------------------------
# Papéis estruturais mutuamente exclusivos (requisito 6)
# ---------------------------------------------------------------------------

class TestPapeisEstruturaisMutuamenteExclusivos:
    def test_calculada_nao_recebe_lancamento_mesmo_se_categoria_bater_por_acaso(self, tmp_path):
        """Uma conta calculada e childless (nao pode ter filhos), mas mesmo
        assim NAO deve aceitar lancamento mapeado ao seu nome -- so a formula
        determina seu valor."""
        linhas = [
            "1;1;;10;Conta A;1;Sim;Não;Categoria A;;",
            "2;1;;20;Calc X;1;Sim;Não;;calculada;+1*1",
        ]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        plano = ler_plano_contas_df(path)
        lancs = [
            {"categoria_origem": "Conta A", "valor": 10.0, "mes_referencia": "2026-01"},
            # lancamento cuja categoria_origem (pos-mapeamento upstream) bate
            # por acaso com a descricao da conta calculada:
            {"categoria_origem": "Calc X", "valor": 999.0, "mes_referencia": "2026-01"},
        ]
        rows = gerar_dre(lancs, plano)
        by_desc = _mapa(rows)
        # Calc X = +1*1 = 10, e NAO 999 (o lancamento "perdido" e diagnosticado
        # como sem-match em conta-folha, nao somado silenciosamente aqui).
        assert by_desc["Calc X"] == 10.0


# ---------------------------------------------------------------------------
# Erros — nenhuma falha silenciosa
# ---------------------------------------------------------------------------

class TestErros:
    def test_id_inexistente(self, tmp_path):
        linhas = ["1;1;;10;Calc;1;Sim;Não;;calculada;+1*999"]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="não existe no plano"):
            ler_plano_contas_df(path)

    def test_formula_vazia(self, tmp_path):
        linhas = ["1;1;;10;Calc;1;Sim;Não;;calculada;"]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="vazia"):
            ler_plano_contas_df(path)

    def test_sintaxe_invalida(self, tmp_path):
        linhas = [
            "1;1;;10;Conta A;1;Sim;Não;Categoria A;;",
            "2;1;;20;Calc;1;Sim;Não;;calculada;1+1",
        ]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="inválido"):
            ler_plano_contas_df(path)

    def test_autorreferencia(self, tmp_path):
        linhas = ["1;1;;10;Calc;1;Sim;Não;;calculada;+1*1"]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="autorreferência"):
            ler_plano_contas_df(path)

    def test_ciclo_a_para_b_para_a(self, tmp_path):
        linhas = [
            "1;1;;10;Calc A;1;Sim;Não;;calculada;+1*2",
            "2;1;;20;Calc B;1;Sim;Não;;calculada;+1*1",
        ]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="circular"):
            ler_plano_contas_df(path)

    def test_calculada_com_categoria_origem(self, tmp_path):
        linhas = ["1;1;;10;Calc;1;Sim;Não;Categoria X;calculada;+1*1"]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="categoria_origem"):
            ler_plano_contas_df(path)

    def test_calculada_agregadora_tem_filhos(self, tmp_path):
        linhas = [
            "1;1;;10;Calc Pai;1;Sim;Não;;calculada;+1*2",
            "2;3;1;20;Folha Filha;1;Sim;Não;Categoria Filha;;",
        ]
        path = _escrever_plano(tmp_path, "plano.csv", linhas)
        with pytest.raises(ValueError, match="agregadora"):
            ler_plano_contas_df(path)


# ---------------------------------------------------------------------------
# parse_formula() isolado
# ---------------------------------------------------------------------------

class TestParseFormula:
    def test_termo_simples(self):
        assert parse_formula("+1*3") == [(1.0, 3)]

    def test_multiplos_termos_com_coeficiente_decimal(self):
        assert parse_formula("+1.5*3 -2*7") == [(1.5, 3), (-2.0, 7)]

    def test_vazia_levanta_erro(self):
        with pytest.raises(ValueError):
            parse_formula("")

    def test_none_levanta_erro(self):
        with pytest.raises(ValueError):
            parse_formula(None)

    def test_sintaxe_sem_asterisco_levanta_erro(self):
        with pytest.raises(ValueError):
            parse_formula("+1x3")
