"""
Integração com Supabase: persiste os resultados do ETL nas tabelas do projeto.

Requer variáveis de ambiente:
    SUPABASE_URL              — URL do projeto Supabase
    SUPABASE_SERVICE_ROLE_KEY — chave de serviço (bypassa RLS)
"""

import datetime
import logging
import math
import os
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500

# Colunas esperadas por tabela (deve bater com o schema do Supabase)
_COLUNAS_LANCAMENTOS = {
    "upload_id", "cliente_id", "mes_referencia", "data_competencia",
    "descricao", "fornecedor_cliente", "categoria_origem", "valor",
    "centro_custo", "valor_total_lancamento", "id_linha_origem",
    "status_validacao", "observacao_etl",
}
_COLUNAS_INCONSISTENCIAS = {
    "upload_id", "mes_referencia", "id_linha_origem", "data_competencia", "descricao",
    "fornecedor_cliente", "valor_total_lancamento", "soma_valores_gerados",
    "diferenca", "observacao",
}
_COLUNAS_RESUMO = {
    # TODO: padronizar schema para incluir cliente_id em resumo_conferencia (migration futura)
    # Vínculo ao cliente: resumo_conferencia.upload_id → uploads.id → uploads.cliente_id
    "upload_id", "mes_referencia", "total_original", "total_etl", "diferenca",
    "qtd_linhas_origem", "qtd_linhas_saida", "qtd_inconsistencias", "qtd_nao_mapeadas",
}
_COLUNAS_DRE = {
    # TODO: padronizar schema para incluir cliente_id em dre_mensal (migration futura)
    "cliente", "mes_referencia", "ordem", "nivel", "descricao", "valor",
}


# ---------------------------------------------------------------------------
# Conversão de datas
# ---------------------------------------------------------------------------

# Formatos aceitos na entrada (tentados em ordem)
_DATE_FORMATS = [
    "%d/%m/%Y",   # 25/01/2026  — padrão brasileiro (Conta Azul / Wingraph)
    "%Y-%m-%d",   # 2026-01-25  — ISO (já correto)
    "%d-%m-%Y",   # 25-01-2026
    "%d.%m.%Y",   # 25.01.2026
    "%Y/%m/%d",   # 2026/01/25
]


def _converter_data(valor: Any) -> tuple[Optional[str], Optional[str]]:
    """
    Converte qualquer representação de data para string ISO YYYY-MM-DD.

    Retorna (iso_str, erro):
      - (iso_str, None)  → conversão OK
      - (None,    erro)  → valor inválido, salvar como NULL
    """
    if valor is None:
        return None, None

    # Já é datetime.date / datetime.datetime
    if isinstance(valor, (datetime.date, datetime.datetime)):
        return valor.strftime("%Y-%m-%d"), None

    # Tenta pd.NaT / NaN
    try:
        if pd.isna(valor):
            return None, None
    except (TypeError, ValueError):
        pass

    texto = str(valor).strip()
    if not texto or texto.lower() in ("none", "nat", "nan", "null"):
        return None, None

    # Já está em formato ISO?
    if len(texto) == 10 and texto[4] == "-" and texto[7] == "-":
        return texto, None

    # Tenta os formatos conhecidos
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(texto, fmt).strftime("%Y-%m-%d"), None
        except ValueError:
            continue

    return None, f"data_competencia inválida: {texto!r}"


def _converter_datas_lancamentos(
    lancamentos: List[dict],
) -> tuple[List[dict], int, int]:
    """
    Percorre os lançamentos convertendo data_competencia para ISO.

    Retorna (lista_modificada, qtd_convertidas, qtd_invalidas).
    Lançamentos com data inválida recebem data_competencia=None e
    o erro é concatenado em observacao_etl.
    """
    convertidas = 0
    invalidas   = 0
    resultado   = []

    for lanc in lancamentos:
        lanc = dict(lanc)   # cópia — não modifica o original
        original = lanc.get("data_competencia")
        iso, erro = _converter_data(original)

        if erro:
            invalidas += 1
            lanc["data_competencia"] = None
            obs_atual = lanc.get("observacao_etl") or ""
            lanc["observacao_etl"] = f"{obs_atual} | {erro}".lstrip(" |")
        else:
            if iso != original and original is not None:
                convertidas += 1
            lanc["data_competencia"] = iso

        resultado.append(lanc)

    return resultado, convertidas, invalidas


# ---------------------------------------------------------------------------
# Sanitização de tipos
# ---------------------------------------------------------------------------

def _sanitizar_valor(v: Any) -> Any:
    """Converte tipos não-serializáveis em tipos nativos Python."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    tipo = type(v).__name__
    if tipo in ("int64", "int32", "int16", "int8"):
        return int(v)
    if tipo in ("float64", "float32"):
        return None if math.isnan(float(v)) else float(v)
    if tipo == "bool_":
        return bool(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _sanitizar_registro(registro: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _sanitizar_valor(v) for k, v in registro.items()}


def _imprimir_contagem_mes(nome: str, registros: List[dict], campo: str = "mes_referencia") -> None:
    from collections import Counter
    contagem = Counter(r.get(campo) for r in registros)
    for mes, qtd in sorted(contagem.items()):
        print(f"    {nome} [{mes or '?'}]: {qtd} registro(s)")


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    ausentes = [v for v, val in [("SUPABASE_URL", url), ("SUPABASE_SERVICE_ROLE_KEY", key)] if not val]
    if ausentes:
        raise EnvironmentError(
            f"Variável(is) de ambiente ausente(s): {', '.join(ausentes)}"
        )
    from supabase import create_client
    return create_client(url, key)


def _localizar_cliente(client, nome: str) -> str:
    """
    Busca ou cria o cliente pelo nome EXATO.

    - Usa eq() para igualdade estrita (não ilike, não parcial).
    - Imprime o res.data bruto para diagnóstico.
    - Verifica que o ID retornado pertence ao nome solicitado.
    - Aborta com RuntimeError se houver qualquer divergência.
    """
    sep = "-" * 60
    print(f"\n{sep}")
    print(f"  LOCALIZAR CLIENTE:")
    print(f"    nome solicitado = {nome!r}")

    # ── SELECT com igualdade exata ─────────────────────────────────────────
    res = client.table("clientes").select("id,nome").eq("nome", nome).execute()

    # Imprime resposta bruta do Supabase (diagnóstico)
    print(f"    RAW res.data    = {res.data}")

    if res.data:
        # Filtra localmente para garantir correspondência exata
        # (proteção extra se o Supabase devolver linhas a mais por RLS/view)
        exatos = [r for r in res.data if r.get("nome") == nome]
        if not exatos:
            raise RuntimeError(
                f"_localizar_cliente: SELECT eq(nome={nome!r}) retornou {res.data} "
                f"mas nenhum registro tem nome exato={nome!r}. "
                "Possível colação case-insensitive ou trigger no banco. ABORTANDO."
            )
        row = exatos[0]
        cliente_id_encontrado = row["id"]

        print(f"    cliente encontrado  = {row['nome']!r}")
        print(f"    cliente_id retornado = {cliente_id_encontrado}")
        print(sep)

        # Assert obrigatório: nome encontrado deve bater com nome solicitado
        if row["nome"] != nome:
            raise RuntimeError(
                f"_localizar_cliente: DIVERGÊNCIA — solicitado={nome!r} retornou nome={row['nome']!r} "
                f"id={cliente_id_encontrado}. ABORTANDO."
            )

        logger.info("Supabase clientes: encontrado — nome=%r | id=%s", nome, cliente_id_encontrado)
        return cliente_id_encontrado

    # ── Não existe → cria novo ────────────────────────────────────────────
    print(f"    → não encontrado, criando novo cliente {nome!r}...")
    ins = client.table("clientes").insert({"nome": nome}).execute()
    print(f"    RAW ins.data    = {ins.data}")

    if not ins.data:
        raise RuntimeError(
            f"_localizar_cliente: INSERT de nome={nome!r} não retornou dados. ABORTANDO."
        )
    novo = ins.data[0]
    novo_id = novo["id"]

    # Verificação round-trip pós-insert
    if novo.get("nome") != nome:
        raise RuntimeError(
            f"_localizar_cliente: INSERT criou nome={novo.get('nome')!r} mas esperava={nome!r}. ABORTANDO."
        )

    print(f"    cliente criado  = {novo['nome']!r}")
    print(f"    cliente_id novo = {novo_id}")
    print(sep)

    logger.info("Supabase clientes: criado — nome=%r | id=%s", nome, novo_id)
    return novo_id


def _contar_exato(client, tabela: str, filtros: dict) -> int:
    """SELECT COUNT(*) com múltiplos filtros eq."""
    q = client.table(tabela).select("id", count="exact")
    for campo, valor in filtros.items():
        q = q.eq(campo, valor)
    res = q.execute()
    return res.count if res.count is not None else len(res.data)


def _contar_por_upload_ids(client, tabela: str, upload_ids: List[str]) -> int:
    """Conta linhas em tabela cujo upload_id está na lista."""
    if not upload_ids:
        return 0
    res = (
        client.table(tabela)
        .select("id", count="exact")
        .in_("upload_id", upload_ids)
        .execute()
    )
    return res.count if res.count is not None else len(res.data)


def _buscar_uploads_existentes(client, cliente_id: str, mes_ref: str) -> List[str]:
    """Retorna todos os upload_ids para cliente + mes_ref."""
    res = (
        client.table("uploads")
        .select("id")
        .eq("cliente_id", cliente_id)
        .eq("mes_referencia", mes_ref)
        .execute()
    )
    return [r["id"] for r in res.data]


def _limpar_processamento(
    client,
    cliente_id: str,
    nome_cliente: str,
    mes_ref: str,
    upload_ids: List[str],
) -> None:
    """
    Apaga explicitamente todos os dados de um processamento anterior.
    SEMPRE usa cliente_id + mes_referencia para garantir isolamento entre clientes.
    Usa IN(...) para deletar em batch — sem depender de CASCADE FK.
    Contagens são verificadas via SELECT COUNT antes e depois de cada delete.
    Ordem garantida: filhos → pais.
    """
    print(f"  Uploads antigos encontrados: {len(upload_ids)} para cliente_id={cliente_id} / {mes_ref}")
    logger.info("Supabase: limpando %d upload(s) antigo(s) para '%s' (id=%s) / %s",
                len(upload_ids), nome_cliente, cliente_id, mes_ref)

    # ── Verificação de segurança: todos os upload_ids pertencem ao cliente_id correto ──
    check = (
        client.table("uploads")
        .select("id,cliente_id")
        .in_("id", upload_ids)
        .execute()
    )
    for row in check.data:
        if row["cliente_id"] != cliente_id:
            raise RuntimeError(
                f"ABORT LIMPEZA: upload_id={row['id']} pertence a cliente_id={row['cliente_id']}, "
                f"mas o cliente atual é cliente_id={cliente_id} ('{nome_cliente}'). "
                "Nunca deletar dados de outro cliente."
            )
    logger.info("Supabase limpeza: todos os %d upload_ids verificados pertencem a cliente_id=%s.",
                len(upload_ids), cliente_id)

    # -- lancamentos --
    antes_lanc = _contar_por_upload_ids(client, "lancamentos", upload_ids)
    client.table("lancamentos").delete().in_("upload_id", upload_ids).execute()
    depois_lanc = _contar_por_upload_ids(client, "lancamentos", upload_ids)
    qtd_lanc = antes_lanc - depois_lanc
    print(f"  Lançamentos removidos: {qtd_lanc} (restam {depois_lanc})")
    if depois_lanc:
        logger.warning("Supabase: %d lançamento(s) NÃO removido(s) — verifique RLS.", depois_lanc)

    # -- inconsistencias --
    antes_inc = _contar_por_upload_ids(client, "inconsistencias", upload_ids)
    client.table("inconsistencias").delete().in_("upload_id", upload_ids).execute()
    depois_inc = _contar_por_upload_ids(client, "inconsistencias", upload_ids)
    qtd_inc = antes_inc - depois_inc

    # -- resumo_conferencia --
    antes_res = _contar_por_upload_ids(client, "resumo_conferencia", upload_ids)
    client.table("resumo_conferencia").delete().in_("upload_id", upload_ids).execute()
    depois_res = _contar_por_upload_ids(client, "resumo_conferencia", upload_ids)
    qtd_res = antes_res - depois_res

    # -- dre_mensal: usa coluna cliente (texto) — schema atual não tem cliente_id
    # TODO: migrar para cliente_id quando schema for padronizado
    antes_dre = _contar_exato(client, "dre_mensal",
                              {"cliente": nome_cliente, "mes_referencia": mes_ref})
    client.table("dre_mensal").delete() \
        .eq("cliente", nome_cliente).eq("mes_referencia", mes_ref).execute()
    depois_dre = _contar_exato(client, "dre_mensal",
                               {"cliente": nome_cliente, "mes_referencia": mes_ref})
    qtd_dre = antes_dre - depois_dre

    # -- uploads (por último) --
    client.table("uploads").delete().in_("id", upload_ids).execute()
    uploads_restantes = _buscar_uploads_existentes(client, cliente_id, mes_ref)
    qtd_up_removidos = len(upload_ids) - len(uploads_restantes)
    print(f"  Uploads removidos: {qtd_up_removidos} (restam {len(uploads_restantes)})")

    if uploads_restantes:
        raise RuntimeError(
            f"Limpeza incompleta: {len(uploads_restantes)} upload(s) ainda existem "
            f"para '{nome_cliente}' (cliente_id={cliente_id}) / {mes_ref}. "
            "Verifique as políticas RLS do Supabase."
        )

    logger.info(
        "Supabase limpeza '%s'/%s concluída: %d upload(s), %d lanc, %d inc, %d resumo, %d dre removidos.",
        nome_cliente, mes_ref,
        qtd_up_removidos, qtd_lanc, qtd_inc, qtd_res, qtd_dre,
    )


def _verificar_e_limpar_mes(
    client,
    cliente_id: str,
    nome_cliente: str,
    mes_ref: str,
    overwrite: bool,
) -> None:
    upload_ids = _buscar_uploads_existentes(client, cliente_id, mes_ref)

    if not upload_ids:
        logger.info("Supabase: nenhum processamento anterior para '%s' / %s.", nome_cliente, mes_ref)
        return

    if not overwrite:
        raise ValueError(
            f"Já existe processamento para '{nome_cliente}' / {mes_ref}.\n"
            "  Rode novamente com --overwrite para substituir."
        )

    _limpar_processamento(client, cliente_id, nome_cliente, mes_ref, upload_ids)


def _criar_upload(client, cliente_id: str, nome_arquivo: str, mes_ref: str, resumo: dict) -> str:
    payload = {
        "cliente_id":        cliente_id,
        "nome_arquivo":      nome_arquivo,
        "mes_referencia":    mes_ref,
        "qtd_linhas_origem": resumo.get("qtd_linhas_origem"),
        "qtd_linhas_saida":  resumo.get("qtd_linhas_saida"),
    }
    res = client.table("uploads").insert(payload).execute()
    upload_id = res.data[0]["id"]

    # Validação defensiva: confirma que o upload recém-criado pertence ao cliente correto
    check = client.table("uploads").select("id,cliente_id,mes_referencia").eq("id", upload_id).execute()
    if not check.data:
        raise RuntimeError(f"upload criado (id={upload_id}) não encontrado imediatamente após insert.")
    row = check.data[0]
    if row["cliente_id"] != cliente_id:
        raise RuntimeError(
            f"upload_id={upload_id} pertence a cliente_id={row['cliente_id']}, "
            f"mas esperava cliente_id={cliente_id}. ABORTANDO."
        )
    if row["mes_referencia"] != mes_ref:
        raise RuntimeError(
            f"upload_id={upload_id} tem mes_referencia={row['mes_referencia']}, "
            f"mas esperava {mes_ref}. ABORTANDO."
        )
    logger.info(
        "Supabase: upload validado — id=%s | cliente_id=%s | mes_ref=%s",
        upload_id, cliente_id, mes_ref,
    )
    return upload_id


def _inserir_em_batches(
    client,
    tabela: str,
    registros: List[dict],
    upload_id: str,
    cliente_id: str = "",
) -> None:
    """
    Insere registros em batches.
    Após o primeiro batch, verifica o que o Supabase efetivamente armazenou
    (detecta triggers/RLS que possam sobrescrever upload_id ou cliente_id).
    """
    total = len(registros)
    print(f"  → {tabela}: {total} registro(s) a inserir")

    # Valida payload antes de qualquer insert
    primeiro = registros[0] if registros else {}
    if primeiro.get("upload_id") and primeiro["upload_id"] != upload_id:
        raise RuntimeError(
            f"[{tabela}] PAYLOAD ERRADO — upload_id no dict ({primeiro['upload_id']}) "
            f"!= upload_id esperado ({upload_id}). ABORTANDO antes de inserir."
        )
    if cliente_id and primeiro.get("cliente_id") and primeiro["cliente_id"] != cliente_id:
        raise RuntimeError(
            f"[{tabela}] PAYLOAD ERRADO — cliente_id no dict ({primeiro['cliente_id']}) "
            f"!= cliente_id esperado ({cliente_id}). ABORTANDO antes de inserir."
        )
    logger.info("Supabase [%s]: %d registro(s) | payload[0]=%s", tabela, total, primeiro)

    for i in range(0, total, _BATCH_SIZE):
        batch = registros[i:i + _BATCH_SIZE]
        try:
            res = client.table(tabela).insert(batch).execute()
        except Exception as exc:
            print(f"  ⚠  Erro ao inserir batch {i + 1}–{i + len(batch)} em '{tabela}': {exc}")
            logger.error("Supabase [%s] batch %d: %s", tabela, i, exc, exc_info=True)
            raise

        # ── Verificação pós-insert: o que o Supabase efetivamente gravou ──
        if res.data:
            gravado = res.data[0]
            gravado_uid = gravado.get("upload_id")
            gravado_cid = gravado.get("cliente_id")
            logger.info(
                "Supabase [%s] batch %d–%d gravado: upload_id=%s | cliente_id=%s",
                tabela, i + 1, i + len(batch), gravado_uid, gravado_cid,
            )
            if gravado_uid is not None and gravado_uid != upload_id:
                raise RuntimeError(
                    f"[{tabela}] DIVERGÊNCIA PÓS-INSERT: "
                    f"enviamos upload_id={upload_id} mas Supabase gravou upload_id={gravado_uid}. "
                    "Verifique triggers ou políticas RLS no banco de dados."
                )
            if cliente_id and gravado_cid is not None and gravado_cid != cliente_id:
                raise RuntimeError(
                    f"[{tabela}] DIVERGÊNCIA PÓS-INSERT: "
                    f"enviamos cliente_id={cliente_id} mas Supabase gravou cliente_id={gravado_cid}. "
                    "Verifique triggers ou políticas RLS no banco de dados."
                )

        logger.info("Supabase [%s]: batch %d–%d inserido (%d linha(s)).",
                    tabela, i + 1, i + len(batch), len(res.data))

    count = _contar_exato(client, tabela, {"upload_id": upload_id})
    print(f"  ✓ {tabela}: {count} linha(s) confirmada(s) no Supabase")


def _mostrar_contagens_finais(
    client, cliente_id: str, nome_cliente: str, upload_id: str, mes_ref: str
) -> None:
    """Consulta e exibe contagens reais no Supabase após todos os inserts."""
    n_up   = _contar_exato(client, "uploads",            {"cliente_id": cliente_id, "mes_referencia": mes_ref})
    n_lanc = _contar_exato(client, "lancamentos",        {"upload_id": upload_id})
    n_inc  = _contar_exato(client, "inconsistencias",    {"upload_id": upload_id})
    n_res  = _contar_exato(client, "resumo_conferencia", {"upload_id": upload_id})
    # dre_mensal: usa coluna cliente (texto) — schema atual não tem cliente_id
    # TODO: migrar para cliente_id quando schema for padronizado
    n_dre  = _contar_exato(client, "dre_mensal",         {"cliente": nome_cliente, "mes_referencia": mes_ref})

    print(f"\n  Contagens finais no Supabase — '{nome_cliente}' (cliente_id={cliente_id}) / {mes_ref}:")
    print(f"    uploads:             {n_up}")
    print(f"    lancamentos:         {n_lanc}  [upload_id={upload_id}]")
    print(f"    inconsistencias:     {n_inc}")
    print(f"    resumo_conferencia:  {n_res}")
    print(f"    dre_mensal:          {n_dre}")

    logger.info(
        "Supabase contagens finais '%s' (id=%s) / %s: uploads=%d lanc=%d inc=%d resumo=%d dre=%d",
        nome_cliente, cliente_id, mes_ref, n_up, n_lanc, n_inc, n_res, n_dre,
    )


def _upsert_plano_contas(client, cliente_id: str, plano_df: pd.DataFrame) -> None:
    if plano_df.empty:
        return
    registros = []
    for _, row in plano_df.iterrows():
        registros.append({
            "cliente_id": cliente_id,
            "id_conta":   int(row["id_conta"]),
            "nivel":      int(row["nivel"]),
            "conta_pai":  None if pd.isna(row["conta_pai"]) else int(row["conta_pai"]),
            "ordem":      int(row["ordem"]),
            "descricao":  str(row["descricao"]),
            "sinal":      int(row["sinal"]),
        })
    for i in range(0, len(registros), _BATCH_SIZE):
        client.table("plano_contas").upsert(
            registros[i:i + _BATCH_SIZE],
            on_conflict="cliente_id,id_conta",
        ).execute()
    logger.info("Supabase: %d linha(s) do plano de contas sincronizada(s).", len(registros))


# ---------------------------------------------------------------------------
# Funções de insert com mapeamento explícito + sanitização
# ---------------------------------------------------------------------------

def _inserir_lancamentos(
    client,
    upload_id: str,
    cliente_id: str,
    lancamentos: List[dict],
    mes_ref: str,
) -> None:
    """
    Insere lançamentos na tabela lancamentos.

    Todos os registros usam EXCLUSIVAMENTE os parâmetros recebidos —
    upload_id e cliente_id capturados localmente, sem nenhuma referência a
    variáveis externas, globais, cache ou singletons.
    """
    # ── Captura local imutável — nenhuma variável externa é referenciada ──
    _uid   = str(upload_id)    # upload_id deste processamento
    _cid   = str(cliente_id)   # cliente_id deste processamento
    _mes   = str(mes_ref)      # mes_referencia deste processamento

    sep = "=" * 70
    print(sep)
    print(f"  PROCESSANDO CLIENTE (lancamentos)")
    print(f"    cliente_id = {_cid}")
    print(f"    upload_id  = {_uid}")
    print(f"    mes_ref    = {_mes}")
    print(f"    qty        = {len(lancamentos)}")
    print(sep)
    logger.info(
        "Supabase [lancamentos] INÍCIO — cliente_id=%s | upload_id=%s | mes_ref=%s | qty=%d",
        _cid, _uid, _mes, len(lancamentos),
    )

    if not lancamentos:
        print("  → lancamentos: nenhum registro.")
        return

    # ── Conversão de datas ANTES de qualquer outra operação ───────────────
    lancamentos, _conv, _inv = _converter_datas_lancamentos(lancamentos)
    print(f"  datas convertidas: {_conv}  |  datas inválidas: {_inv}")
    if _inv:
        logger.warning(
            "Supabase [lancamentos]: %d data(s) inválida(s) salva(s) como NULL "
            "(detalhe em observacao_etl).", _inv
        )
    logger.info("Supabase [lancamentos]: datas — convertidas=%d | inválidas=%d", _conv, _inv)

    # ── Monta registros usando SOMENTE variáveis locais capturadas acima ──
    registros: List[dict] = []
    for lanc in lancamentos:
        reg = {
            "upload_id":              _uid,   # local — nunca vem de cache
            "cliente_id":             _cid,   # local — nunca vem de cache
            "mes_referencia":         _mes,
            "data_competencia":       lanc.get("data_competencia"),
            "descricao":              lanc.get("descricao"),
            "fornecedor_cliente":     lanc.get("fornecedor_cliente"),
            "categoria_origem":       lanc.get("categoria_origem"),
            "valor":                  lanc.get("valor"),
            "centro_custo":           lanc.get("centro_custo"),
            "valor_total_lancamento": lanc.get("valor_total_lancamento"),
            "id_linha_origem":        lanc.get("id_linha_origem"),
            "status_validacao":       lanc.get("status_validacao"),
            "observacao_etl":         lanc.get("observacao_etl"),
        }
        registros.append(_sanitizar_registro(reg))

    # ── Confirmação explícita do payload ANTES de qualquer insert ──────────
    primeiro = registros[0]
    print(f"  ANTES DO INSERT — payload[0]:")
    print(f"    payload upload_id  = {primeiro['upload_id']}")
    print(f"    payload cliente_id = {primeiro['cliente_id']}")
    print(f"    payload mes_ref    = {primeiro['mes_referencia']}")

    # Assert para detectar qualquer shadowing ou mutação inesperada
    if primeiro["upload_id"] != _uid:
        raise RuntimeError(
            f"[lancamentos] ASSERT FALHOU: payload upload_id={primeiro['upload_id']} "
            f"!= _uid={_uid}. Possível mutação de variável. ABORTANDO."
        )
    if primeiro["cliente_id"] != _cid:
        raise RuntimeError(
            f"[lancamentos] ASSERT FALHOU: payload cliente_id={primeiro['cliente_id']} "
            f"!= _cid={_cid}. Possível mutação de variável. ABORTANDO."
        )

    logger.info(
        "Supabase [lancamentos] payload validado — upload_id=%s | cliente_id=%s",
        primeiro["upload_id"], primeiro["cliente_id"],
    )

    _inserir_em_batches(client, "lancamentos", registros, _uid, cliente_id=_cid)


def _inserir_inconsistencias(client, upload_id: str, inconsistencias: List[dict], mes_ref: str) -> None:
    if not inconsistencias:
        print("  → inconsistencias: nenhum registro.")
        return

    registros = []
    for inc in inconsistencias:
        reg = {
            "upload_id":              upload_id,
            "mes_referencia":         mes_ref,
            "id_linha_origem":        inc.get("id_linha_origem"),
            "data_competencia":       inc.get("data_competencia"),
            "descricao":              inc.get("descricao"),
            "fornecedor_cliente":     inc.get("fornecedor_cliente"),
            "valor_total_lancamento": inc.get("valor_total_lancamento"),
            "soma_valores_gerados":   inc.get("soma_valores_gerados"),
            "diferenca":              inc.get("diferenca"),
            "observacao":             inc.get("observacao"),
        }
        extras = set(reg.keys()) - _COLUNAS_INCONSISTENCIAS
        if extras:
            logger.warning("inconsistencias: colunas inesperadas ignoradas: %s", extras)
        registros.append(_sanitizar_registro(reg))

    _inserir_em_batches(client, "inconsistencias", registros, upload_id)


def _inserir_resumo(client, upload_id: str, cliente_id: str, mes_ref: str, resumo: dict) -> None:
    """
    Insere resumo de conferência.

    Schema atual: resumo_conferencia NÃO possui cliente_id.
    Vínculo ao cliente via: resumo_conferencia.upload_id → uploads.id → uploads.cliente_id
    TODO: padronizar schema para incluir cliente_id em resumo_conferencia (migration futura).
    """
    reg = {
        # NÃO inclui cliente_id — coluna não existe no schema atual
        "upload_id":           upload_id,
        "mes_referencia":      mes_ref,
        "total_original":      resumo.get("total_original"),
        "total_etl":           resumo.get("total_etl"),
        "diferenca":           resumo.get("diferenca"),
        "qtd_linhas_origem":   resumo.get("qtd_linhas_origem"),
        "qtd_linhas_saida":    resumo.get("qtd_linhas_saida"),
        "qtd_inconsistencias": resumo.get("qtd_inconsistencias"),
        "qtd_nao_mapeadas":    resumo.get("qtd_nao_mapeadas"),
    }
    extras = set(reg.keys()) - _COLUNAS_RESUMO
    if extras:
        logger.warning("resumo_conferencia: colunas inesperadas ignoradas: %s", extras)

    payload = _sanitizar_registro(reg)
    print(f"  → resumo_conferencia: 1 registro | upload_id={upload_id}")
    logger.info("Supabase [resumo_conferencia]: payload=%s", payload)
    try:
        client.table("resumo_conferencia").insert(payload).execute()
        print("  ✓ resumo_conferencia: 1 linha confirmada no Supabase")
    except Exception as exc:
        print(f"  ⚠  Erro ao inserir resumo_conferencia: {exc}")
        logger.error("Supabase [resumo_conferencia]: %s", exc, exc_info=True)
        raise


def _inserir_dre_mensal(
    client, cliente_id: str, nome_cliente: str, mes_ref: str, dre_rows: List[dict]
) -> None:
    """
    Insere linhas do DRE mensal.

    Schema atual: dre_mensal usa coluna 'cliente' (TEXT), não cliente_id.
    TODO: padronizar schema para incluir cliente_id em dre_mensal (migration futura).
    """
    if not dre_rows:
        print("  → dre_mensal: nenhum registro.")
        return

    logger.info("Supabase [dre_mensal]: cliente='%s' | mes_ref=%s | qty=%d",
                nome_cliente, mes_ref, len(dre_rows))

    registros = []
    for r in dre_rows:
        reg = {
            # NÃO inclui cliente_id — coluna não existe no schema atual
            "cliente":        nome_cliente,   # TEXT — isolamento por nome
            "mes_referencia": mes_ref,
            "ordem":          r.get("ordem"),
            "nivel":          r.get("nivel"),
            "descricao":      r.get("descricao"),
            "valor":          r.get("valor"),
        }
        extras = set(reg.keys()) - _COLUNAS_DRE
        if extras:
            logger.warning("dre_mensal: colunas inesperadas ignoradas: %s", extras)
        registros.append(_sanitizar_registro(reg))

    total = len(registros)
    print(f"  → dre_mensal: {total} registro(s) | cliente={nome_cliente!r} | mes_ref={mes_ref}")
    logger.info("Supabase [dre_mensal]: %d registro(s) | exemplo: %s", total, registros[0])

    for i in range(0, total, _BATCH_SIZE):
        batch = registros[i:i + _BATCH_SIZE]
        try:
            res = client.table("dre_mensal").insert(batch).execute()
            logger.info("Supabase [dre_mensal]: batch %d–%d inserido (%d linha(s)).",
                        i + 1, i + len(batch), len(res.data))
        except Exception as exc:
            print(f"  ⚠  Erro ao inserir batch {i + 1}–{i + len(batch)} em 'dre_mensal': {exc}")
            logger.error("Supabase [dre_mensal] batch %d: %s", i, exc, exc_info=True)
            raise

    # Confirmação via coluna cliente (texto) — schema atual
    count = _contar_exato(client, "dre_mensal", {"cliente": nome_cliente, "mes_referencia": mes_ref})
    print(f"  ✓ dre_mensal: {count} linha(s) confirmada(s) no Supabase")


# ---------------------------------------------------------------------------
# Helpers públicos (usados por outros módulos do ETL)
# ---------------------------------------------------------------------------

def buscar_cliente_id(nome_cliente: str) -> Optional[str]:
    """
    Retorna o cliente_id do Supabase para o nome dado.

    Usado por etl/mapeamentos.py para carregar mapeamentos sem exigir
    --save-supabase. Retorna None se Supabase não está configurado.
    """
    try:
        client = _get_client()
        return _localizar_cliente(client, nome_cliente)
    except EnvironmentError:
        return None
    except Exception as exc:
        logger.warning("buscar_cliente_id('%s'): %s", nome_cliente, exc)
        return None


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------

def salvar_no_supabase(
    nome_cliente: str,
    nome_arquivo: str,
    lancamentos: List[dict],
    inconsistencias: List[dict],
    resumo: dict,
    plano_df: pd.DataFrame,
    dre_rows: Optional[List[dict]] = None,
    mes_ref: Optional[str] = None,
    overwrite: bool = False,
) -> None:
    """
    Persiste todos os resultados do ETL no Supabase.

    Regra: para cada cliente + mes_referencia existe apenas um processamento válido.
    Com --overwrite os dados anteriores são apagados explicitamente antes da inserção.

    Args:
        nome_cliente:    Nome do cliente (ex.: "Zixbe").
        nome_arquivo:    Caminho/nome do arquivo Excel de entrada.
        lancamentos:     Lista de lançamentos padronizados.
        inconsistencias: Lista de inconsistências encontradas.
        resumo:          Dict com totais do resumo_conferencia.
        plano_df:        DataFrame do plano de contas (pode ser vazio).
        dre_rows:        Linhas do DRE mensal (pode ser vazio).
        mes_ref:         Mês de referência no formato AAAA-MM (obrigatório).
        overwrite:       Se True, remove dados anteriores do mesmo cliente/mês.
    """
    if not mes_ref:
        raise ValueError("mes_ref é obrigatório para salvar no Supabase. Use --mes-ref AAAA-MM.")
    if not nome_cliente or not nome_cliente.strip():
        raise ValueError("nome_cliente está vazio ao entrar em salvar_no_supabase. Verificar pipeline.py.")

    # ── Checkpoint 5: nome_cliente que chegou ao save ─────────────────────
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  CLIENTE RECEBIDO NO SUPABASE SAVE: {nome_cliente!r}")
    print(f"  mes_ref recebido: {mes_ref!r}")
    print(sep)

    # ── Cria cliente Supabase NOVO — sem reutilização de estado ───────────
    client = _get_client()

    # ── Busca cliente_id EXCLUSIVAMENTE pelo nome recebido nesta chamada ──
    _cliente_id = _localizar_cliente(client, nome_cliente)
    _mes_ref    = str(mes_ref)

    # ── Assert cruzado: confirma que o ID não pertence a outro cliente ────
    # Faz reverse-lookup: SELECT nome FROM clientes WHERE id = _cliente_id
    _rev = client.table("clientes").select("id,nome").eq("id", _cliente_id).execute()
    if not _rev.data:
        raise RuntimeError(
            f"salvar_no_supabase: cliente_id={_cliente_id} não existe na tabela clientes. ABORTANDO."
        )
    _rev_nome = _rev.data[0]["nome"]
    if _rev_nome != nome_cliente:
        raise RuntimeError(
            f"salvar_no_supabase: ALERTA CRÍTICO — nome_cliente={nome_cliente!r} mas "
            f"cliente_id={_cliente_id} pertence a {_rev_nome!r}. "
            "ID de outro cliente sendo usado. ABORTANDO sem salvar nada."
        )

    print(f"\n{sep}")
    print(f"  PROCESSANDO CLIENTE:")
    print(f"    cliente_nome  = {nome_cliente!r}")
    print(f"    cliente_id    = {_cliente_id}")
    print(f"    reverse-check = {_rev_nome!r}  ✓")
    print(f"    mes_ref       = {_mes_ref}")
    print(f"    overwrite     = {overwrite}")
    print(sep)
    logger.info(
        "Supabase: INÍCIO — cliente_nome='%s' | cliente_id=%s | mes_ref=%s | overwrite=%s",
        nome_cliente, _cliente_id, _mes_ref, overwrite,
    )

    # Verificação e limpeza — levanta ValueError se existir e overwrite=False
    _verificar_e_limpar_mes(client, _cliente_id, nome_cliente, _mes_ref, overwrite)

    # ── Cria upload e captura upload_id localmente ────────────────────────
    _upload_id = _criar_upload(client, _cliente_id, nome_arquivo, _mes_ref, resumo)
    print(f"\n  upload criado:")
    print(f"    upload_id  = {_upload_id}")
    print(f"    cliente_id = {_cliente_id}  ← deve ser {nome_cliente}")
    print(f"    mes_ref    = {_mes_ref}")
    logger.info(
        "Supabase: upload criado — upload_id=%s | cliente_id=%s | mes_ref=%s",
        _upload_id, _cliente_id, _mes_ref,
    )

    print(f"\n  Registros a salvar (mes_referencia={_mes_ref}):")
    print(f"    lancamentos:         {len(lancamentos)}")
    print(f"    inconsistencias:     {len(inconsistencias)}")
    print(f"    dre_mensal:          {len(dre_rows or [])}")
    print(f"    resumo_conferencia:  1")

    print()
    _upsert_plano_contas(client, _cliente_id, plano_df)
    _inserir_lancamentos(client, _upload_id, _cliente_id, lancamentos, _mes_ref)
    _inserir_inconsistencias(client, _upload_id, inconsistencias, _mes_ref)
    _inserir_resumo(client, _upload_id, _cliente_id, _mes_ref, resumo)
    _inserir_dre_mensal(client, _cliente_id, nome_cliente, _mes_ref, dre_rows or [])

    # Reatribui para o restante da função usar os mesmos nomes
    cliente_id = _cliente_id
    upload_id  = _upload_id
    mes_ref    = _mes_ref

    _mostrar_contagens_finais(client, cliente_id, nome_cliente, upload_id, mes_ref)

    # ── Pós-validação: upload_ids em lancamentos filtrado por cliente_id + mes ──
    res_val = (
        client.table("lancamentos")
        .select("upload_id")
        .eq("cliente_id", cliente_id)
        .eq("mes_referencia", mes_ref)
        .execute()
    )
    uploads_no_mes = {r["upload_id"] for r in res_val.data}
    print(f"\n  Pós-validação — upload_ids em lancamentos para cliente_id={cliente_id} / {mes_ref}:")
    for uid in sorted(uploads_no_mes):
        marker = "✓ CORRETO" if uid == upload_id else "⚠  INESPERADO"
        print(f"    {uid}  [{marker}]")
    inesperados = uploads_no_mes - {upload_id}
    if inesperados:
        logger.warning(
            "Supabase ALERTA: upload_ids inesperados em lancamentos para cliente_id=%s / %s: %s",
            cliente_id, mes_ref, inesperados,
        )
        print(f"  ⚠  ALERTA: {len(inesperados)} upload_id(s) inesperado(s) encontrado(s)!")
    else:
        logger.info(
            "Supabase pós-validação OK: único upload_id em lancamentos para cliente_id=%s / %s = %s",
            cliente_id, mes_ref, upload_id,
        )
        print("  ✓ Pós-validação OK: todos os lançamentos usam o upload_id correto.")

    # ── Validação final: distribuição de lancamentos por cliente_id + mes + upload ──
    print(f"\n  Validação GROUP BY lancamentos (mes_referencia={mes_ref}):")
    res_grp = (
        client.table("lancamentos")
        .select("cliente_id, mes_referencia, upload_id")
        .eq("mes_referencia", mes_ref)
        .execute()
    )
    from collections import Counter
    grp: Counter = Counter()
    for r in res_grp.data:
        grp[(r["cliente_id"], r["mes_referencia"], r["upload_id"])] += 1
    print(f"    {'cliente_id':<38}  {'mes':<8}  {'upload_id':<38}  {'count':>6}")
    print(f"    {'-'*38}  {'-'*8}  {'-'*38}  {'-'*6}")
    for (cid, mes, uid), cnt in sorted(grp.items()):
        marker = " ← ESTE" if cid == cliente_id and uid == upload_id else ""
        print(f"    {cid:<38}  {mes:<8}  {uid:<38}  {cnt:>6}{marker}")
    logger.info("Supabase GROUP BY validação: %d combinação(ões) distintas em lancamentos para mes=%s",
                len(grp), mes_ref)
