"""services/user_service.py — Direitos do titular (LGPD, Art. 18)

Centraliza operações sobre a conta/ dados do usuário exigidas pela LGPD:

  * `delete_user_account`  -> direito ao esquecimento (hard delete transacional
                             de todos os registros + remoção de PDFs em disco).
  * `export_user_data`     -> portabilidade (download completo em JSON).

As operações são sempre escopadas por `user_id` (isolamento multi-tenant) e
rodam em transação única (commit/rollback) para nunca deixar dados parciais.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tabelas cujas linhas pertencem a um usuário (escopadas por user_id).
# A exclusão manual é explícita/transacional, independentemente de o FK usar
# `ON DELETE CASCADE` (defensivo e auditável).
_USER_SCOPED_TABLES = (
    "rubricas_holerite",
    "holerites",
    "user_profiles",
    "user_profile_history",
    "ai_usage_logs",
    "ai_blocked_logs",
    "parse_errors",
)


def _user_exists(db, user_id: int) -> bool:
    return db.execute("SELECT id FROM users WHERE id = ?", [user_id]).fetchone() is not None


def delete_user_account(db, user_id: int) -> int:
    """Hard delete transacional de TODOS os dados do usuário.

    Apaga holerites, rubricas, perfil, histórico de perfil, logs de IA (uso e
    bloqueios) e erros de parsing; por fim remove a conta em `users`. Depois do
    commit, remove os PDFs do holerite persistidos em disco (LGPD).

    Returns:
        int: quantidade de arquivos em disco removidos.
    """
    if not _user_exists(db, user_id):
        raise ValueError("Usuário não encontrado.")

    paths = [
        row["file_path"]
        for row in db.execute(
            "SELECT file_path FROM holerites "
            "WHERE user_id = ? AND file_path IS NOT NULL AND TRIM(file_path) != ''",
            [user_id],
        ).fetchall()
    ]

    try:
        for table in _USER_SCOPED_TABLES:
            db.execute(f"DELETE FROM {table} WHERE user_id = ?", [user_id])
        db.execute("DELETE FROM users WHERE id = ?", [user_id])
        db.commit()
    except Exception:
        db.rollback()
        raise

    removed = 0
    for raw_path in paths:
        try:
            file = Path(raw_path)
            if file.exists():
                file.unlink()
                removed += 1
        except OSError:
            logger.warning("Não foi possível remover o arquivo %s", raw_path)
    return removed


def export_user_data(db, user_id: int) -> dict:
    """Monta o payload JSON de portabilidade do usuário (LGPD Art. 18/19).

    Retorna os paystubs com totais e rubricas itemizadas + métricas agregadas,
    escopados estritamente por `user_id`.
    """
    import json
    from datetime import datetime

    user = db.execute(
        "SELECT id, name, email, role, plan, is_active, created_at "
        "FROM users WHERE id = ?",
        [user_id],
    ).fetchone()

    rows = db.execute(
        """
        SELECT
            h.id                 AS holerite_id,
            h.company_name,
            h.company_tax_id,
            h.mes_referencia,
            h.tipo_documento,
            h.file_hash,
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

    paystubs: dict = {}
    for row in rows:
        hid = row["holerite_id"]
        block = paystubs.get(hid)
        if block is None:
            totals = {}
            if row["totals"]:
                try:
                    totals = json.loads(row["totals"])
                except (json.JSONDecodeError, TypeError):
                    totals = {}
            block = {
                "holerite_id": hid,
                "mes_referencia": row["mes_referencia"],
                "tipo_documento": row["tipo_documento"],
                "company_name": row["company_name"],
                "company_tax_id": row["company_tax_id"],
                "file_hash": row["file_hash"],
                "totals": totals,
                "rubricas": [],
            }
            paystubs[hid] = block
        if row["rubrica_codigo"] is not None or row["rubrica_descricao"]:
            block["rubricas"].append(
                {
                    "codigo": row["rubrica_codigo"],
                    "descricao": row["rubrica_descricao"],
                    "tipo": str(row["rubrica_tipo"] or "").upper(),
                    "valor": float(row["rubrica_valor"] or 0.0),
                    "referencia": row["rubrica_referencia"],
                }
            )

    metrics = {}
    try:
        from services.analytics_service import get_advanced_analytics, get_user_totals
        metrics["kpis"] = get_user_totals(user_id)
        metrics["advanced"] = get_advanced_analytics(user_id)
    except Exception:  # noqa: BLE001 - best-effort; o export não quebra por métricas
        logger.exception("Falha ao agregar métricas para export do usuário %s", user_id)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "plan": user["plan"],
            "is_active": bool(user["is_active"]),
            "created_at": user["created_at"],
        } if user else {},
        "paystubs": list(paystubs.values()),
        "metrics": metrics,
    }
