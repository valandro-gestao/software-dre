"""
Testes de regressão e evolução do motor de DRE (etl/dre.py).

Estratégia:
- Regressão: para os planos hoje em uso (Zixbe, Zeus legado — ambos com no
  máximo 3 níveis), o resultado de gerar_dre() é comparado contra uma
  implementação de referência escrita de forma independente (mesma
  especificação documentada em etl/dre.py, código diferente), cobrindo
  TODAS as contas do plano — não só uma amostra escolhida a dedo.
- Evolução: para o plano novo do Zeus (9 colunas, com nível 4 e contas com
  múltiplas linhas de categoria_origem), testes dedicados verificam nível 4,
  folha em nível 2 e ausência de dupla contagem.

Os CSVs em tests/fixtures/ são cópias congeladas dos arquivos reais no
momento em que este teste foi escrito — não são os arquivos "vivos" do
projeto (cfg/clientes/... ou ~/Downloads/...), para o teste não depender
do ambiente nem mudar de resultado se aqueles arquivos forem editados.

Execute com: python -m pytest tests/test_dre.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etl.plano_contas import ler_plano_contas_df, ler_mapeamento_plano, _validar_plano
from etl.dre import gerar_dre, _FORMULAS_RESULTADO

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Implementação de referência independente (não reaproveita o rollup do
# código em teste) — usada só para comparação cruzada nos testes de
# regressão dos planos de 3 níveis.
# ---------------------------------------------------------------------------

def _referencia_gerar_dre(lancamentos, plano_df):
    df_lanc = pd.DataFrame(lancamentos)
    df_lanc = df_lanc[df_lanc["categoria_origem"].notna()]
    meses = sorted(df_lanc["mes_referencia"].dropna().unique())

    filhos_de = {}
    for _, row in plano_df.iterrows():
        pai = row["conta_pai"]
        if pd.notna(pai):
            filhos_de.setdefault(int(pai), set()).add(int(row["id_conta"]))

    descricao_por_id = {}
    sinal_por_id = {}
    for _, row in plano_df.iterrows():
        idc = int(row["id_conta"])
        descricao_por_id[idc] = row["descricao"]
        sinal_por_id[idc] = row["sinal"]

    resultado = []
    for mes in meses:
        df_mes = df_lanc[df_lanc["mes_referencia"] == mes]
        soma_cat = df_mes.groupby("categoria_origem")["valor"].sum().to_dict()

        valor_base_cache = {}

        def calc_valor_base(id_conta):
            if id_conta in valor_base_cache:
                return valor_base_cache[id_conta]
            filhos = filhos_de.get(id_conta)
            if filhos:
                v = sum(calc_valor_base(f) for f in filhos)
            else:
                v = soma_cat.get(descricao_por_id[id_conta], 0.0)
            valor_base_cache[id_conta] = v
            return v

        for idc in descricao_por_id:
            calc_valor_base(idc)

        valor_final_por_id = {
            idc: round(valor_base_cache[idc] * sinal_por_id[idc], 2)
            for idc in descricao_por_id
        }

        valor_por_desc = {}
        for idc, desc in descricao_por_id.items():
            valor_por_desc[desc] = valor_final_por_id[idc]

        for descricao, componentes in _FORMULAS_RESULTADO.items():
            total = round(sum(op * valor_por_desc.get(dep, 0.0) for op, dep in componentes), 2)
            for idc, desc in descricao_por_id.items():
                if desc == descricao:
                    valor_final_por_id[idc] = total
            valor_por_desc[descricao] = total

        for _, row in plano_df.sort_values("ordem").iterrows():
            idc = int(row["id_conta"])
            resultado.append({
                "mes": mes,
                "ordem": int(row["ordem"]),
                "nivel": int(row["nivel"]),
                "descricao": row["descricao"],
                "valor": valor_final_por_id[idc],
            })
    return resultado


def _lancamentos_para_todas_as_folhas_estruturais(plano_df, mes="2026-01"):
    """1 lançamento sintético por conta-folha estrutural única (sem
    filhos), cobrindo a árvore inteira do plano — inclusive linhas que só
    passam a ser folhas válidas no modelo NOVO (ex.: contas childless fora
    do nível 3 que não são linha de fórmula). Valor natural = id_conta
    (positivo), determinístico e fácil de rastrear em caso de falha.

    Usado só para o autoteste do modelo genérico (comparação entre
    gerar_dre() e a referência independente, ambos usando a mesma noção
    de folha). NÃO usar para comparar contra o comportamento do motor
    ATUAL/antigo (nivel==3) — ver _lancamentos_para_todas_as_folhas_nivel3."""
    ids_pai = set(int(x) for x in plano_df["conta_pai"].dropna())
    vistos = set()
    lancs = []
    for _, row in plano_df.iterrows():
        idc = int(row["id_conta"])
        if idc in ids_pai or idc in vistos:
            continue
        vistos.add(idc)
        lancs.append({
            "categoria_origem": row["descricao"],
            "valor": float(idc),
            "mes_referencia": mes,
        })
    return lancs


def _lancamentos_para_todas_as_folhas_nivel3(plano_df, mes="2026-01"):
    """1 lançamento sintético por descricao única de nível 3, cobrindo só
    o que o motor ATUAL (mask3 = nivel==3) já é capaz de receber hoje.

    Usado nos testes de regressão (Zixbe, Zeus legado): o objetivo desses
    testes é provar que, para entradas realmente possíveis com o motor de
    hoje, o motor novo produz o mesmo resultado. Contas childless fora do
    nível 3 que não são linha de fórmula (ex.: 'SALDO RESERVA' no plano
    legado do Zeus) nunca recebem categoria_origem mapeada na prática --
    nenhum mecanismo de mapeamento hoje produz esse valor -- por isso são
    deliberadamente excluídas aqui; incluí-las descreveria uma mudança de
    comportamento real (o motor novo passa a tratá-las como folha), não
    uma regressão."""
    vistos = set()
    lancs = []
    for _, row in plano_df[plano_df["nivel"] == 3].iterrows():
        idc = int(row["id_conta"])
        if idc in vistos:
            continue
        vistos.add(idc)
        lancs.append({
            "categoria_origem": row["descricao"],
            "valor": float(idc),
            "mes_referencia": mes,
        })
    return lancs


def _mapa_por_descricao(rows):
    """{descricao: valor} — assume 1 valor por descricao (vale para os
    planos testados: nenhuma descricao duplicada tem valores diferentes)."""
    return {r["descricao"]: r["valor"] for r in rows}


# ---------------------------------------------------------------------------
# Regressão — Zixbe (plano real de 3 níveis, formato antigo, 6 colunas)
# ---------------------------------------------------------------------------

class TestRegressaoZixbe:
    def setup_method(self):
        self.plano = ler_plano_contas_df(str(FIXTURES / "plano_contas_zixbe.csv"))
        assert not self.plano.empty
        self.lancs = _lancamentos_para_todas_as_folhas_nivel3(self.plano)

    def test_todas_as_linhas_batem_com_referencia_independente(self):
        atual = _mapa_por_descricao(gerar_dre(self.lancs, self.plano))
        referencia = _mapa_por_descricao(_referencia_gerar_dre(self.lancs, self.plano))
        assert atual == referencia

    def test_totais_formula_conferidos_a_mao(self):
        atual = _mapa_por_descricao(gerar_dre(self.lancs, self.plano))
        # RECEITA BRUTA = soma de tudo nivel 3 sob RECEITAS OPERACIONAIS
        # (soma dos ids das 5 contas-folha: 3,4,5,6,7)
        assert atual["RECEITA BRUTA"] == float(3 + 4 + 5 + 6 + 7)


# ---------------------------------------------------------------------------
# Regressão — Zeus legado (plano real de 3 níveis, 8 colunas, sem
# categoria_origem — é o arquivo hoje efetivamente carregado em produção
# via fallback ~/Downloads/plano_contas_zeus.csv)
# ---------------------------------------------------------------------------

class TestRegressaoZeusLegado:
    def setup_method(self):
        self.plano = ler_plano_contas_df(str(FIXTURES / "plano_contas_zeus_legado.csv"))
        assert not self.plano.empty
        self.lancs = _lancamentos_para_todas_as_folhas_nivel3(self.plano)

    def test_todas_as_linhas_batem_com_referencia_independente(self):
        atual = _mapa_por_descricao(gerar_dre(self.lancs, self.plano))
        referencia = _mapa_por_descricao(_referencia_gerar_dre(self.lancs, self.plano))
        assert atual == referencia


# ---------------------------------------------------------------------------
# Zeus novo (9 colunas, nível 4, contas com múltiplas linhas de
# categoria_origem) — plano ainda NÃO promovido a canônico. Carregado
# aqui só pelo caminho explícito da fixture, nunca via resolver_plano_contas
# nem via main.py --client zeus (que cairia no fallback ~/Downloads).
# ---------------------------------------------------------------------------

class TestZeusNovo:
    def setup_method(self):
        self.plano = ler_plano_contas_df(str(FIXTURES / "plano_contas_zeus_novo.csv"))
        assert not self.plano.empty
        mask_dre = self.plano["exibir_dre"].fillna(True).astype(bool)
        self.plano_dre = self.plano[mask_dre].copy()

    def test_todas_as_linhas_batem_com_referencia_independente(self):
        lancs = _lancamentos_para_todas_as_folhas_estruturais(self.plano_dre)
        atual = _mapa_por_descricao(gerar_dre(lancs, self.plano_dre))
        referencia = _mapa_por_descricao(_referencia_gerar_dre(lancs, self.plano_dre))
        assert atual == referencia

    def test_nivel4_inss_fgts_soma_em_encargos_da_folha(self):
        # gerar_dre() casa por `descricao` (id=76 tem descricao "INSS / IRRF",
        # categoria_origem CSV="INSS" -- essa coluna já foi consumida na
        # etapa de mapeamento upstream; o lançamento aqui chega com o valor
        # já mapeado, isto é, categoria_origem == descricao de destino).
        lancs = [
            {"categoria_origem": "INSS / IRRF", "valor": -300.0, "mes_referencia": "2026-01"},
            {"categoria_origem": "FGTS", "valor": -200.0, "mes_referencia": "2026-01"},
        ]
        atual = _mapa_por_descricao(gerar_dre(lancs, self.plano_dre))
        assert atual["Encargos da Folha"] == 500.0

    def test_distribuicao_aos_socios_nivel2_recebe_valor_direto(self):
        """id=122, nivel=2, tem categoria_origem preenchido e nao tem filhos
        -- e uma folha real fora do nivel 3. sinal=+1 no plano (mesma
        convenção das contas de receita: sinal natural == sinal exibido)."""
        lancs = [
            {"categoria_origem": "Distribuição aos Sócios", "valor": 400.0, "mes_referencia": "2026-01"},
        ]
        atual = _mapa_por_descricao(gerar_dre(lancs, self.plano_dre))
        assert atual["Distribuição aos Sócios"] == 400.0

    def test_conta_com_multiplas_linhas_categoria_origem_nao_duplica_no_pai(self):
        """id=72 'Salarios' tem 2 linhas no CSV (categoria_origem
        'Participação de lucros' e 'Salários e Antecipações'), ambas
        mapeando para a mesma descricao 'Salários'. DESPESAS COM PESSOAL
        (pai) nao deve contar o valor de Salarios duas vezes."""
        lancs = [
            {"categoria_origem": "Salários", "valor": -1000.0, "mes_referencia": "2026-01"},
        ]
        atual = _mapa_por_descricao(gerar_dre(lancs, self.plano_dre))
        assert atual["Salários"] == 1000.0
        assert atual["DESPESAS COM PESSOAL"] == 1000.0


# ---------------------------------------------------------------------------
# Validação de estrutura — agregador com categoria_origem preenchido é
# inválido (não deve ser tratado como "valor próprio + filhos").
# Fixture sintética porque nenhum plano real hoje tem esse caso.
# ---------------------------------------------------------------------------

_PLANO_INVALIDO_CSV = (
    "id_conta;nivel;conta_pai;ordem;descricao;sinal;exibir_dre;auditoria_only;categoria_origem\n"
    '"1;1;;10;GRUPO;1;Sim;Não;"\n'
    '"2;2;1;20;AGREGADOR COM MAPEAMENTO;1;Sim;Não;Categoria Ambígua"\n'
    '"3;3;2;30;Folha Normal;1;Sim;Não;Categoria Normal"\n'
)


@pytest.fixture()
def plano_invalido_path(tmp_path):
    p = tmp_path / "plano_invalido.csv"
    p.write_text(_PLANO_INVALIDO_CSV, encoding="utf-8")
    return str(p)


class TestValidacaoAgregadorComCategoriaOrigem:
    def test_ler_mapeamento_plano_ignora_categoria_origem_de_agregador(self, plano_invalido_path):
        mapeamento = ler_mapeamento_plano(plano_invalido_path)
        assert "Categoria Normal" in mapeamento
        assert "Categoria Ambígua" not in mapeamento

    def test_validar_plano_registra_estrutura_invalida(self, plano_invalido_path, caplog):
        import logging
        df = ler_plano_contas_df(plano_invalido_path)
        with caplog.at_level(logging.ERROR):
            _validar_plano(df, plano_invalido_path)
        assert any(
            "AGREGADOR COM MAPEAMENTO" in rec.message or "agregador" in rec.message.lower()
            for rec in caplog.records
        )
