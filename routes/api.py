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

from flask import Blueprint, current_app, jsonify, request, send_file

from database.connection import get_db
from routes.auth import admin_required, current_user_id, login_required
from services import analytics_service
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

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "Formato inválido. Envie um PDF."}), 400

    content = file.read()

    # Evita documentos duplicados por hash (por usuário).
    file_hash = compute_file_hash(content)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM holerites WHERE user_id = ? AND file_hash = ?",
        [user_id, file_hash],
    ).fetchone()
    if existing:
        return jsonify({"error": "Este holerite já foi importado."}), 409

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
    """Auditoria de paystubs e detecção de anomalias."""
    user_id = current_user_id()
    return jsonify(analytics_service.audit_paystub_anomalies(user_id))


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

