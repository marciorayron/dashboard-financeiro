"""
api.py
------
Blueprint responsável pelas rotas de API (formato JSON).

Endpoints (todos protegidos por `login_required` e escopados pelo
`session['user_id']` — isolamento multi-tenant real):

  * POST /api/upload               -> envia um holerite (PDF), parseia e salva.
  * GET  /api/summary              -> KPIs agregados do usuário.
  * GET  /api/monthly              -> série temporal mensal (bruto x líquido).
  * GET  /api/descontos            -> descontos por rubrica.
  * GET  /api/empresas             -> lista de empregadores do usuário.
  * GET  /api/analytics/advanced   -> métricas avançadas (overtime, taxas, base).
  * GET  /api/analytics/audit      -> auditoria/anomalias de paystubs.
  * POST /api/analytics/ask-ai     -> pergunta livre à IA (contextualizada).
  * POST /api/analytics/explain-anomaly -> explicação de inconsistência com IA.
  * GET  /api/plan                 -> plano (Free/Pro) e consumo do usuário.
  * POST /api/admin/fix-classification -> corrige rubricas 'provento' e recalcula totais.
  * GET  /api/holerites            -> lista os holerites do usuário.
  * GET  /api/holerites/<id>       -> detalhe (rubricas + texto bruto).
  * DELETE /api/holerites/<id>     -> exclui um holerite do usuário.
  * GET  /api/export?format=...    -> exporta holerites/rubricas em xlsx ou csv.

O `user_id` NUNCA vem de parâmetros do cliente: é sempre lido da sessão.
"""
import json
import logging
import traceback
import uuid
from io import BytesIO
from pathlib import Path

import pandas as pd

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
    send_file,
    session,
)

from database.connection import get_db
from routes.auth import admin_required, current_user_id, login_required
from services import ai_service, analytics_service
from services.ai_service import (
    AIValidationError,
    PromptInjectionError,
    ask_ai as ai_ask_question,
    enforce_ai_rate_limit,
    explain_anomaly as ai_explain_anomaly,
    get_ai_usage_summary,
    log_ai_usage,
    log_blocked_attempt,
)
from services.auth_service import (
    PaystubLimitError,
    enforce_paystub_upload_limit,
)
from services.db_service import get_catalog_map
from services.deepseek_service import (
    _MANDATORY_DESCONTO,
    _MANDATORY_PROVENTO,
    enrich_paycheck_data,
)
from services.pdf_parser import (
    compute_file_hash,
    extract_line_items_pdf,
    extract_text_from_bytes,
)

logger = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _allowed_file(filename: str) -> bool:
    """Verifica se a extensão do arquivo é permitida (pdf)."""
    allowed = current_app.config.get("ALLOWED_EXTENSIONS", {"pdf"})
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def _has_pdf_magic(content: bytes) -> bool:
    """
    Verifica a assinatura real de um PDF ('%PDF-') no início do conteúdo.

    Proteção contra MIME spoofing: um arquivo apenas renomeado para '.pdf'
    (ex.: script/HTML/binary) não avança para o parsing — é rejeitado antes
    com HTTP 400, sem ser persistido nem registrado como erro de parsing.
    Toleramos apenas espaços/linhas em branco iniciais (headers legítimos).
    """
    if not content:
        return False
    return content.lstrip()[:5] == b"%PDF-"


def _json_dumps(data) -> str:
    """Serializa um dicionário para JSON (para colunas TEXT do SQLite)."""
    return json.dumps(data, ensure_ascii=False)


def _json_loads(raw):
    """Desserializa JSON de uma coluna TEXT, com fallback seguro."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _to_float(value) -> float:
    """Converte um valor arbitrário em float com fallback seguro."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _load_holerite_or_404(holerite_id: int, user_id: int):
    """Busca um holerite garantindo que pertença ao usuário logado."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM holerites WHERE id = ? AND user_id = ?",
        [holerite_id, user_id],
    ).fetchone()
    return row


def _limit_error_response(exc):
    """
    Converte exceções de limite/validação (AI / plano) em resposta JSON.

    Usado para traduzir `PaystubLimitError`, `RateLimitError`,
    `QueryTooLongError` e `PromptInjectionError` nos códigos HTTP corretos
    (402/429/400) com o payload de erro padronizado.
    """
    payload = getattr(exc, "payload", None)
    status = getattr(exc, "status_code", 400)
    return jsonify(payload() if callable(payload) else {"error": "ERRO", "message": str(exc)}), status


def _catalog_enforced_tipo(db, codigo) -> str:
    """
    Retorna o tipo ('PROVENTO'/'DESCONTO') forçado pelo catálogo para um código.

    Aplica primeiro os códigos obrigatórios (hard-coded) e depois o catálogo do
    banco. Se nada casar, devolve None (o tipo armazenado é mantido).
    """
    raw = str(codigo or "").strip().upper()
    if raw in _MANDATORY_DESCONTO:
        return "DESCONTO"
    if raw in _MANDATORY_PROVENTO:
        return "PROVENTO"
    catalog = get_catalog_map(db)
    tipo = catalog.get(raw) or catalog.get(raw.lstrip("/"))
    return tipo or None


# ---------------------------------------------------------------------
# Upload de holerites
# ---------------------------------------------------------------------
@api_bp.route("/upload", methods=["POST"])
@login_required
def upload_holerite():
    """
    Recebe um PDF de holerite, extrai o texto, envia à DeepSeek para
    parsing universal (com fallback local) e persiste no banco.

    Request multipart: campo `file` (obrigatório).
    """
    user_id = current_user_id()

    # Política Freemium: usuário do plano Gratuito tem teto de 3 holerites.
    try:
        enforce_paystub_upload_limit(get_db(), user_id)
    except PaystubLimitError as exc:
        return _limit_error_response(exc)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "Formato inválido. Envie um PDF."}), 400

    content = file.read()

    # Validação de conteúdo (anti-spoofing / MIME falso): só aceita bytes que
    # realmente comecem com a assinatura de PDF. Um arquivo arbitrário apenas
    # renomeado para '.pdf' é rejeitado com HTTP 400 ANTES de qualquer parsing.
    if not _has_pdf_magic(content):
        return (
            jsonify(
                {
                    "error": "Arquivo inválido. O conteúdo enviado não é um PDF válido."
                }
            ),
            400,
        )

    # Evita documentos duplicados por hash (por usuário).
    file_hash = compute_file_hash(content)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM holerites WHERE user_id = ? AND file_hash = ?",
        [user_id, file_hash],
    ).fetchone()
    if existing:
        return jsonify({"error": "Este holerite já foi importado."}), 409

    # O PDF persistido em disco é tratado como arquivo temporário do pipeline:
    # se a transação NÃO for concluída (falha/rollback), ele é removido no
    # `finally` — nunca deixamos um PDF órfão no disco (LGPD/minimização).
    stored_path = None
    committed = False
    try:
        # 1) Extrai o texto bruto do PDF.
        raw_text = extract_text_from_bytes(content)

        # 1.1) Extração posicional (pdfplumber + coordenadas x0/x1): tipos por
        #      coluna (Proventos/Descontos), determinístico.
        position_items = extract_line_items_pdf(content) if content else []

        # 2) Parsing universal via DeepSeek (com fallback local garantido).
        data = enrich_paycheck_data(
            raw_text,
            api_key=current_app.config.get("DEEPSEEK_API_KEY"),
            base_url=current_app.config.get("DEEPSEEK_BASE_URL"),
            line_items_override=position_items,
            filename=file.filename,
        )

        # 3) Persiste o arquivo em disco.
        upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{user_id}_{uuid.uuid4().hex}.pdf"
        stored_path = upload_dir / stored_name
        stored_path.write_bytes(content)

        totals = {
            "base_salary": data.get("base_salary", 0.0),
            "total_earnings": data.get("total_earnings", 0.0),
            "total_deductions": data.get("total_deductions", 0.0),
            "net_value": data.get("net_value", 0.0),
        }

        # 4) Insere o holerite.
        cur = db.execute(
            """
            INSERT INTO holerites
                (user_id, company_name, company_tax_id, mes_referencia,
                 tipo_documento, totals, bases, file_path, file_hash, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                user_id,
                data.get("company_name") or "Empresa Desconhecida",
                data.get("company_tax_id"),
                data.get("reference_month"),
                data.get("doc_type") or "FOLHA_MENSAL",
                _json_dumps(totals),
                _json_dumps({"ai_parsed": bool(current_app.config.get("DEEPSEEK_API_KEY"))}),
                str(stored_path),
                file_hash,
                raw_text,
            ],
        )
        holerite_id = cur.lastrowid

        # 5) Insere as rubricas itemizadas (line_items).
        for item in data.get("line_items", []):
            db.execute(
                """
                INSERT INTO rubricas_holerite
                    (holerite_id, user_id, codigo, descricao, tipo, valor)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    holerite_id,
                    user_id,
                    item.get("code"),
                    item.get("description"),
                    str(item.get("type", "PROVENTO")).lower(),
                    item.get("amount", 0.0),
                ],
            )

        db.commit()
        committed = True
        return jsonify(
            {
                "message": "Holerite importado com sucesso.",
                "id": holerite_id,
                "company_name": data.get("company_name"),
                "reference_month": data.get("reference_month"),
                "doc_type": data.get("doc_type"),
                "totals": totals,
            }
        ), 201

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Falha ao processar upload")
        # Auditoria: registra o erro de parsing com contexto do usuário para
        # que o admin possa reanalisar/corrigir no painel `/admin`.
        from services.monitoring_service import record_parse_error

        try:
            record_parse_error(
                db,
                user_id=user_id,
                filename=file.filename,
                error_type=type(exc).__name__,
                error_message=str(exc),
                traceback=traceback.format_exc(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar erro de parsing no log")
        return jsonify({"error": f"Falha ao processar: {exc}"}), 500
    finally:
        # Remove o arquivo temporário do pipeline quando a transação não foi
        # concluída (commit falhou/rollback). Em sucesso o PDF é mantido no
        # armazenamento oficial referenciado por `holerites.file_path`.
        if stored_path is not None and not committed and stored_path.exists():
            try:
                stored_path.unlink()
            except OSError:
                logger.warning(
                    "Não foi possível remover o PDF temporário %s", stored_path
                )


# ---------------------------------------------------------------------
# Consultas do dashboard
# ---------------------------------------------------------------------
@api_bp.route("/summary", methods=["GET"])
@login_required
def summary():
    """KPIs agregados do usuário (proventos, descontos, líquido, alíquota)."""
    user_id = current_user_id()
    mes = request.args.get("mes")
    return jsonify(analytics_service.get_user_totals(user_id, mes))


@api_bp.route("/monthly", methods=["GET"])
@login_required
def monthly():
    """Série temporal mensal (bruto x líquido) para o gráfico de linha."""
    user_id = current_user_id()
    company = request.args.get("company")
    return jsonify(analytics_service.get_monthly_series(user_id, company))


@api_bp.route("/descontos", methods=["GET"])
@login_required
def descontos():
    """Descontos agregados por rubrica (gráfico de barras/pizza)."""
    user_id = current_user_id()
    return jsonify(analytics_service.get_descontos_por_rubrica(user_id))


@api_bp.route("/empresas", methods=["GET"])
@login_required
def empresas():
    """Lista de empregadores do usuário para o filtro do dashboard."""
    user_id = current_user_id()
    return jsonify(analytics_service.get_empresas(user_id))


@api_bp.route("/analytics/advanced", methods=["GET"])
@login_required
def analytics_advanced():
    """
    Métricas avançadas do usuário, formatadas para Plotly.

    Suporta os mesmos filtros dos demais endpoints:
        * `mes`     -> filtra por competência (YYYY-MM).
        * `company` -> filtra por empregador.

    Returns:
        JSON com `meta`, `overtime`, `tax_rates` e `base_vs_variable`.
    """
    user_id = current_user_id()
    mes = request.args.get("mes")
    company = request.args.get("company")
    return jsonify(
        analytics_service.get_advanced_analytics(
            user_id,
            mes_referencia=mes,
            company_name=company,
        )
    )


@api_bp.route("/analytics/overtime-impact", methods=["GET"])
@login_required
def analytics_overtime_impact():
    """Simulador de horas extras: impacto marginal de INSS/IRRF e líquido."""
    from models.profile import load_profile

    user_id = current_user_id()
    db = get_db()
    profile = load_profile(db, user_id)
    try:
        hours = float(request.args.get("hours", "0") or 0)
    except ValueError:
        hours = 0.0
    try:
        multiplier = float(request.args.get("multiplier", "1.5") or 1.5)
    except ValueError:
        multiplier = 1.5
    return jsonify(
        analytics_service.calculate_overtime_impact(profile, hours, multiplier)
    )


@api_bp.route("/analytics/audit", methods=["GET"])
@login_required
def analytics_audit():
    """Auditoria de paystubs e detecção de anomalias (escopo mes/company)."""
    user_id = current_user_id()
    return jsonify(
        analytics_service.audit_paystub_anomalies(
            user_id,
            mes_referencia=request.args.get("mes"),
            company_name=request.args.get("company"),
        )
    )


@api_bp.route("/plan", methods=["GET"])
@login_required
def get_plan():
    """Informações do plano e de consumo do usuário (para o frontend)."""
    user_id = current_user_id()
    db = get_db()
    from services.auth_service import get_paystub_limit, count_user_paystubs

    plan = ai_service.get_user_plan(db, user_id)
    paystub_limit = get_paystub_limit(db, plan)
    return jsonify(
        {
            "plan": plan,
            "paystub_count": count_user_paystubs(db, user_id),
            "paystub_limit": paystub_limit,
            "ai": get_ai_usage_summary(db, user_id),
        }
    )


@api_bp.route("/analytics/explain-anomaly", methods=["POST"])
@login_required
def analytics_explain_anomaly():
    """
    Explica uma inconsistência específica com auxílio de IA.

    Corpo JSON: objeto de anomalia (ex.: categoria, baseline, delta/título,
    descrição, mês, impacto monetário). Retorna uma explicação curta (2 frases)
    com ação recomendada.
    """
    user_id = current_user_id()
    db = get_db()
    body = request.get_json(silent=True) or {}
    if not body or not isinstance(body, dict):
        return jsonify({"error": "Anomalia inválida."}), 400

    try:
        # Rate limiting (Freemium) antes de consumir tokens da IA.
        enforce_ai_rate_limit(db, user_id)
        explanation = ai_explain_anomaly(
            body,
            api_key=current_app.config.get("DEEPSEEK_API_KEY"),
            base_url=current_app.config.get("DEEPSEEK_BASE_URL"),
        )
        log_ai_usage(db, user_id)  # registra apenas chamadas válidas
    except AIValidationError as exc:
        if isinstance(exc, PromptInjectionError):
            log_blocked_attempt(
                db, user_id, "prompt_injection",
                detail=json.dumps(body, ensure_ascii=False)[:200],
            )
        return _limit_error_response(exc)

    return jsonify({"explanation": explanation})


@api_bp.route("/analytics/ask-ai", methods=["POST"])
@login_required
def analytics_ask_ai():
    """
    Pergunta livre à IA, contextualizada com o resumo dos holerites do usuário.

    Corpo JSON: {"question": "..."} (máx. 200 caracteres). Retorna uma
    resposta concisa baseada nos dados reais do dashboard.
    """
    user_id = current_user_id()
    db = get_db()
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()

    try:
        # Valida tamanho/injeção ANTES de consumir tokens.
        from services.ai_service import sanitize_query
        sanitize_query(question)
        # Rate limiting (Freemium) antes de consumir tokens da IA.
        enforce_ai_rate_limit(db, user_id)
        # Contexto rico: breakdown mês a mês + agregados (permite perguntas
        # temporais como "qual o menor/maior salário e em quais meses?").
        # LGPD: anonimiza PII (nome, CPF, empresa etc.) antes de enviar à IA.
        context = ai_service.anonymize_payload(
            analytics_service.build_ai_paystub_context(user_id)
        )
        answer = ai_ask_question(
            question,
            context=context,
            api_key=current_app.config.get("DEEPSEEK_API_KEY"),
            base_url=current_app.config.get("DEEPSEEK_BASE_URL"),
        )
        log_ai_usage(db, user_id)  # registra apenas chamadas válidas
    except AIValidationError as exc:
        if isinstance(exc, PromptInjectionError):
            log_blocked_attempt(
                db, user_id, "prompt_injection",
                detail=(question or "")[:200],
            )
        return _limit_error_response(exc)

    return jsonify({"answer": answer})


@api_bp.route("/analytics/explain-card", methods=["POST"])
@login_required
def analytics_explain_card():
    """Explica um card com IA (consumindo crédito Freemium/Pro)."""
    from services.ai_service import (
        RateLimitError,
        enforce_ai_rate_limit,
        explain_card as ai_explain_card,
        get_ai_cooldown_seconds,
        get_ai_usage_summary,
        log_ai_usage,
    )

    user_id = current_user_id()
    body = request.get_json(silent=True) or {}
    card_id = (body.get("card_id") or "").strip()
    if not card_id:
        return jsonify({"status": "invalid_card", "message": "Informe o card_id."}), 400

    db = get_db()
    # 1) Checa a cota IA do plano ANTES de qualquer cálculo (429 se estourou).
    try:
        enforce_ai_rate_limit(db, user_id)
    except RateLimitError:
        cooldown = get_ai_cooldown_seconds(db, user_id)
        return (
            jsonify(
                {
                    "status": "rate_limit_exceeded",
                    "message": "Você atingiu o limite de consultas de IA para o seu plano.",
                    "retry_after_seconds": cooldown,
                    **get_ai_usage_summary(db, user_id),
                }
            ),
            429,
        )

    # 2) Monta o payload escopado do card.
    try:
        result = analytics_service.get_card_explanation(
            db,
            user_id,
            card_id,
            mes_referencia=(body.get("month") or "").strip() or None,
            company_name=(body.get("company") or "").strip() or None,
        )
    except ValueError:
        return jsonify(
            {"status": "invalid_card", "message": "Card não suportado para explicação."}
        ), 400

    # LGPD: garante que o payload do card que segue à IA não contém PII.
    result = ai_service.anonymize_payload(result)

    # 3) Invoca a IA (best-effort) quando chave configurada; offline mantém o
    #    texto determinístico. Consome 1 crédito da cota do plano.
    api_key = current_app.config.get("DEEPSEEK_API_KEY")
    if api_key:
        refined = ai_explain_card(
            result["title"],          # rótulo human-readable, NUNCA a chave crua
            result["markdown"],       # contexto com os valores numéricos reais
            api_key=api_key,
            base_url=current_app.config.get("DEEPSEEK_BASE_URL"),
        )
        if refined and not refined.startswith("Não foi possível"):
            result["markdown"] = analytics_service.humanize_explanation(refined)
    log_ai_usage(db, user_id)

    # Saneamento final de qualquer saída (determinística ou da IA).
    markdown = analytics_service.humanize_explanation(result["markdown"])

    return jsonify(
        {
            "status": "ok",
            "card_id": result["card_id"],
            "title": result["title"],
            "markdown": markdown,
            **get_ai_usage_summary(db, user_id),
        }
    )


@api_bp.route("/analytics/inconsistency-breakdown", methods=["GET"])
@login_required
def analytics_inconsistency_breakdown():
    """Quebra itemizada do total R$ das inconsistências (modal / relatório)."""
    user_id = current_user_id()
    return jsonify(analytics_service.get_inconsistency_breakdown(user_id))


@api_bp.route("/analytics/tax-projection", methods=["GET"])
@login_required
def analytics_tax_projection():
    """Projeção tributária anual acumulada (INSS & IRRF) até o fim do ano.

    Query params:
        * `method`: 'average' (média YTD), 'trend' (últimos 3M) ou
          'last_month' (run-rate do último paystub). Padrão: 'average'.
    """
    user_id = current_user_id()
    method = request.args.get("method", "average")
    return jsonify(analytics_service.get_tax_projection(user_id, method=method))


@api_bp.route("/analytics/projection", methods=["GET"])
@login_required
def analytics_projection():
    """Projeção financeira anual (13º, férias, PPR)."""
    from models.profile import load_profile

    user_id = current_user_id()
    db = get_db()
    profile = load_profile(db, user_id)

    # PPR médio histórico (net das folhas PPR).
    ppr_avg = 0.0
    rows = db.execute(
        "SELECT totals FROM holerites WHERE user_id=? AND tipo_documento='PPR'",
        [user_id],
    ).fetchall()
    if rows:
        import json as _json
        nets = []
        for r in rows:
            try:
                t = _json.loads(r["totals"])
                nets.append(float(t.get("net_value") or 0.0))
            except (ValueError, TypeError):
                continue
        ppr_avg = (sum(nets) / len(nets)) if nets else 0.0

    historical = {"ppr_avg": ppr_avg}
    return jsonify(
        analytics_service.get_annual_financial_projection(profile, historical)
    )


@api_bp.route("/admin/fix-classification", methods=["POST"])
@admin_required
def fix_classification():
    """
    (Re)executa a correção retroativa da classificação das rubricas.

    Varre `rubricas_holerite`, força `tipo='desconto'` em deduções gravadas
    como 'provento' e recalcula os totais dos holerites afetados. Idempotente
    e útil para sanear dados históricos sem reiniciar o servidor.

    Returns:
        JSON: {"message", "scanned", "fixed", "recalculated"}
    """
    from services.db_service import (
        fix_classification_from_catalog,
        fix_provento_classification,
        init_rubrica_catalog,
    )

    db = get_db()
    seeded = init_rubrica_catalog(db)
    # Keyword (heurística) antes, catálogo (autoritativo) por último.
    keyword = fix_provento_classification(db)
    catalog = fix_classification_from_catalog(db)
    return jsonify(
        {
            "message": "Classificação corrigida pelo catálogo oficial.",
            "seeded": seeded["upserted"],
            "keyword": keyword,
            "catalog": catalog,
        }
    )


@api_bp.route("/holerites", methods=["GET"])
@login_required
def list_holerites():
    """Lista os holerites do usuário (sem o texto bruto, por performance)."""
    user_id = current_user_id()
    db = get_db()
    rows = db.execute(
        """
        SELECT id, company_name, company_tax_id, mes_referencia,
               tipo_documento, totals, bases, parsed_at
        FROM holerites
        WHERE user_id = ?
        ORDER BY mes_referencia DESC,
          CASE
            WHEN tipo_documento = 'FOLHA_MENSAL' THEN 1
            WHEN tipo_documento = 'PPR' THEN 2
            ELSE 3
          END ASC,
          id DESC
        """,
        [user_id],
    ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["totals"] = _json_loads(item.get("totals"))
        item["bases"] = _json_loads(item.get("bases"))
        items.append(item)

    return jsonify(items)


@api_bp.route("/holerites/<int:holerite_id>", methods=["GET"])
@login_required
def holerite_detail(holerite_id: int):
    """Detalhe de um holerite: rubricas itemizadas + texto bruto."""
    user_id = current_user_id()
    row = _load_holerite_or_404(holerite_id, user_id)
    if row is None:
        return jsonify({"error": "Holerite não encontrado."}), 404

    db = get_db()
    rubricas = db.execute(
        """
        SELECT codigo, descricao, tipo, valor
        FROM rubricas_holerite
        WHERE holerite_id = ? AND user_id = ?
        ORDER BY id ASC
        """,
        [holerite_id, user_id],
    ).fetchall()

    # Enforcement on-the-fly: o tipo retornado ao frontend é forçado pelo
    # catálogo (maiúsculo, 'PROVENTO'/'DESCONTO'), garantindo que o modal
    # exiba o badge correto independentemente do estado armazenado.
    line_items = []
    for r in rubricas:
        item = dict(r)
        enforced = _catalog_enforced_tipo(db, r["codigo"])
        if enforced is not None:
            item["tipo"] = enforced
        else:
            item["tipo"] = str(r["tipo"]).upper()
        line_items.append(item)

    return jsonify(
        {
            "id": row["id"],
            "company_name": row["company_name"],
            "company_tax_id": row["company_tax_id"],
            "mes_referencia": row["mes_referencia"],
            "tipo_documento": row["tipo_documento"],
            "totals": _json_loads(row["totals"]),
            "bases": _json_loads(row["bases"]),
            "parsed_at": row["parsed_at"],
            "line_items": line_items,
            "raw_text": row["raw_text"],
        }
    )


@api_bp.route("/holerites/<int:holerite_id>", methods=["DELETE"])
@login_required
def holerite_delete(holerite_id: int):
    """Exclui um holerite e suas rubricas (apenas do usuário logado)."""
    user_id = current_user_id()
    row = _load_holerite_or_404(holerite_id, user_id)
    if row is None:
        return jsonify({"error": "Holerite não encontrado."}), 404

    db = get_db()
    db.execute(
        "DELETE FROM holerites WHERE id = ? AND user_id = ?",
        [holerite_id, user_id],
    )
    db.commit()

    # Remove o arquivo em disco, se existir e pertencer ao usuário.
    if row["file_path"]:
        try:
            path = Path(row["file_path"])
            if path.exists():
                path.unlink()
        except OSError:
            logger.warning("Não foi possível remover o arquivo %s", row["file_path"])

    return jsonify({"message": "Holerite excluído.", "id": holerite_id})


# ---------------------------------------------------------------------
# Direitos do titular (LGPD, Art. 18) — portabilidade & esquecimento
# ---------------------------------------------------------------------
@api_bp.route("/user/account", methods=["DELETE"])
@login_required
def user_delete_account():
    """Hard delete transacional da conta e de todos os dados do usuário logado.

    Remove paystubs, rubricas, perfil/histórico, logs de IA e erros de parse,
    além dos PDFs em disco — depois limpa a sessão (LGPD, Art. 18 - exclusão).
    """
    from services.user_service import delete_user_account

    user_id = current_user_id()
    db = get_db()
    try:
        delete_user_account(db, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao excluir a conta do usuário %s", user_id)
        return jsonify({"error": "Não foi possível excluir a conta."}), 500
    session.clear()
    return jsonify({"message": "Conta e dados pessoais excluídos."}), 200


@api_bp.route("/user/export-data", methods=["GET"])
@login_required
def user_export_data():
    """Exporta todos os dados do usuário logado (portabilidade LGPD, Art. 18)."""
    from services.user_service import export_user_data

    payload = export_user_data(get_db(), current_user_id())
    return jsonify(payload)


# ---------------------------------------------------------------------
# Exportação de dados
# ---------------------------------------------------------------------
# MIME types corretos por formato de exportação.
_EXPORT_MIMETYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}



def _export_file(user_id, fmt):
    """Gera e envia o arquivo de exportação do usuário (xlsx ou csv)."""
    allowed = current_app.config.get("EXPORT_ALLOWED_EXTENSIONS", {"xlsx", "csv"})
    if fmt not in allowed or fmt not in _EXPORT_MIMETYPES:
        return jsonify({"error": "Formato inválido. Use 'xlsx' ou 'csv'."}), 400

    db = get_db()
    rows = db.execute(
        """
        SELECT
            h.id                 AS holerite_id,
            h.company_name,
            h.company_tax_id,
            h.mes_referencia,
            h.tipo_documento,
            h.totals,
            r.codigo             AS rubrica_codigo,
            r.descricao          AS rubrica_descricao,
            r.tipo               AS rubrica_tipo,
            r.valor              AS rubrica_valor,
            r.referencia         AS rubrica_referencia
        FROM holerites h
        LEFT JOIN rubricas_holerite r ON r.holerite_id = h.id
        WHERE h.user_id = ?
        ORDER BY h.mes_referencia ASC, h.id ASC, r.id ASC
        """,
        [user_id],
    ).fetchall()

    columns = [
        "holerite_id",
        "company_name",
        "company_tax_id",
        "mes_referencia",
        "tipo_documento",
        "base_salary",
        "total_earnings",
        "total_deductions",
        "net_value",
        "rubrica_codigo",
        "rubrica_descricao",
        "rubrica_tipo",
        "rubrica_valor",
        "rubrica_referencia",
    ]
    records = []
    for row in rows:
        totals = _json_loads(row["totals"])
        records.append(
            {
                "holerite_id": row["holerite_id"],
                "company_name": row["company_name"],
                "company_tax_id": row["company_tax_id"],
                "mes_referencia": row["mes_referencia"],
                "tipo_documento": row["tipo_documento"],
                "base_salary": _to_float(totals.get("base_salary")),
                "total_earnings": _to_float(totals.get("total_earnings")),
                "total_deductions": _to_float(totals.get("total_deductions")),
                "net_value": _to_float(totals.get("net_value")),
                "rubrica_codigo": row["rubrica_codigo"],
                "rubrica_descricao": row["rubrica_descricao"],
                "rubrica_tipo": str(row["rubrica_tipo"] or "").upper(),
                "rubrica_valor": _to_float(row["rubrica_valor"]),
                "rubrica_referencia": row["rubrica_referencia"],
            }
        )

    df = pd.DataFrame(records, columns=columns)
    buf = BytesIO()

    if fmt == "xlsx":
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="holerites")
        buf.seek(0)
        mimetype = _EXPORT_MIMETYPES["xlsx"]
        download_name = "holerites_export.xlsx"
    else:
        # utf-8-sig (BOM) para compatibilidade com Excel/Planilhas no Windows.
        buf.write(df.to_csv(index=False).encode("utf-8-sig"))
        buf.seek(0)
        mimetype = _EXPORT_MIMETYPES["csv"]
        download_name = "holerites_export.csv"

    return send_file(
        buf,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
    )


@api_bp.route("/export", methods=["GET"])
@login_required
def export_data():
    """Exporta holerites/rubricas do usuário logado em xlsx ou csv."""
    fmt = (request.args.get("format") or "xlsx").lower()
    return _export_file(current_user_id(), fmt)


@api_bp.route("/export/excel", methods=["GET"])
@login_required
def export_excel():
    """Atalho para exportação em planilha XLSX."""
    return _export_file(current_user_id(), "xlsx")


@api_bp.route("/export/csv", methods=["GET"])
@login_required
def export_csv():
    """Atalho para exportação em CSV."""
    return _export_file(current_user_id(), "csv")


@api_bp.route("/admin/claim-holerites", methods=["POST"])
@admin_required
def admin_claim_holerites():
    """Migração/conserto de propriedade: associa holerites órfãos ao admin."""
    from database.queries import claim_holerites_for_admin

    db = get_db()
    result = claim_holerites_for_admin(db)
    return jsonify({"message": "Propriedade de holerites sincronizada.", **result})



@api_bp.route("/admin/assign-holerites", methods=["POST"])
@admin_required
def admin_assign_holerites():
    """
    Reatribui os holerites de teste a um usuário específico (ex.: 'marcio').

    Corpo JSON: {"email": "marcio@rayron.com"}. Permite ao admin devolver os
    holerites a uma conta para visualização/demonstração, invertendo a
    migração padrão para a conta admin.
    """
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Informe o e-mail do usuário-alvo."}), 400

    db = get_db()
    target = db.execute(
        "SELECT id FROM users WHERE email = ?", [email]
    ).fetchone()
    if target is None:
        return jsonify({"error": "Usuário não encontrado."}), 404

    from database.queries import assign_holerites_to_user

    result = assign_holerites_to_user(db, target["id"])
    return jsonify(
        {
            "message": "Holerites reatribuídos para " + email + ".",
            **result,
        }
    )

