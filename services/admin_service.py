"""
services/admin_service.py
--------------------------
Serviço de agregação e governança para o painel administrativo (`/admin`).

Centraliza as consultas analíticas de backoffice, desacopladas das rotas:

  * ``get_stats``            -> KPIs globais (usuários, planos, holerites,
                                consumo de IA, armazenamento, erros de parsing).
  * ``get_ai_usage``         -> FinOps: breakdown diário, leaderboard e log de
                                tentativas bloqueadas (prompt injection).
  * ``reset_user_usage``     -> zera o consumo de IA de um usuário (suporte).
  * ``get_storage_breakdown``/``clean_orphaned_files`` -> gestão de disco.
  * ``list_users_with_stats``-> serializa usuários com contagens associadas.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import List

from flask import current_app

from models.user import list_users, to_dict as user_to_dict
from services import monitoring_service, settings_service
from services.ai_service import (
    count_queries_in_window,
    ensure_ai_blocked_logs_table,
    ensure_ai_usage_logs_table,
)

# Estimativas de tokens por consulta (heurística documentada). O painel usa
# estes valores para FinOps até que contadores exatos de tokens (input/output)
# sejam persistidos em `ai_usage_logs`.
EST_INPUT_TOKENS_PER_QUERY = 300
EST_OUTPUT_TOKENS_PER_QUERY = 100


def estimate_ai_cost(input_tokens: float, output_tokens: float, cfg: dict) -> dict:
    """
    Calcula o custo estimado de IA a partir das tarifas configuradas.

    Custo = (input_tokens / 1e6 * input_rate) + (output_tokens / 1e6 * output_rate)
    """
    cfg = cfg or settings_service.DEFAULT_AI_CONFIG
    input_rate = float(cfg.get("input_rate_per_million") or 0)
    output_rate = float(cfg.get("output_rate_per_million") or 0)
    input_cost = (float(input_tokens) / 1_000_000) * input_rate
    output_cost = (float(output_tokens) / 1_000_000) * output_rate
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "estimated_tokens": int(input_tokens) + int(output_tokens),
        "input_cost": round(input_cost, 4),
        "output_cost": round(output_cost, 4),
        "estimated_cost": round(input_cost + output_cost, 4),
        "input_rate_per_million": round(input_rate, 8),
        "output_rate_per_million": round(output_rate, 8),
        "model": cfg.get("model", ""),
    }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_stats(db) -> dict:
    """Retorna KPIs globais agregados para a barra de resumo do admin."""
    ensure_ai_usage_logs_table(db)
    ensure_ai_blocked_logs_table(db)

    total_users = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    free_users = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE plan = 'free'"
    ).fetchone()["c"]
    pro_users = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE plan = 'pro'"
    ).fetchone()["c"]
    active_users = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_active = 1"
    ).fetchone()["c"]
    admins = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
    ).fetchone()["c"]

    total_paystubs = db.execute(
        "SELECT COUNT(*) AS c FROM holerites"
    ).fetchone()["c"]

    ai_total = db.execute(
        "SELECT COUNT(*) AS c FROM ai_usage_logs"
    ).fetchone()["c"]
    ai_24h = db.execute(
        "SELECT COUNT(*) AS c FROM ai_usage_logs WHERE created_at >= ?",
        [(_now_utc() - timedelta(seconds=24 * 60 * 60)).strftime("%Y-%m-%d %H:%M:%S")],
    ).fetchone()["c"]
    ai_1h = db.execute(
        "SELECT COUNT(*) AS c FROM ai_usage_logs WHERE created_at >= ?",
        [(_now_utc() - timedelta(seconds=60 * 60)).strftime("%Y-%m-%d %H:%M:%S")],
    ).fetchone()["c"]
    blocked_total = db.execute(
        "SELECT COUNT(*) AS c FROM ai_blocked_logs"
    ).fetchone()["c"]

    # Estimativa de tokens (input/output) e custo (FinOps) usando as tarifas
    # do modelo ativo configurado no painel (`ai_config`).
    ai_config = settings_service.get_ai_config(db)
    input_tokens = int(ai_total) * EST_INPUT_TOKENS_PER_QUERY
    output_tokens = int(ai_total) * EST_OUTPUT_TOKENS_PER_QUERY
    ai_cost = estimate_ai_cost(input_tokens, output_tokens, ai_config)

    unresolved_errors = monitoring_service.count_unresolved_parse_errors(db)

    # Armazenamento (PDFs persistidos).
    pdf_rows = db.execute(
        "SELECT file_path FROM holerites WHERE file_path IS NOT NULL AND TRIM(file_path) != ''"
    ).fetchall()
    storage_bytes = 0
    for r in pdf_rows:
        try:
            if os.path.exists(r["file_path"]):
                storage_bytes += os.path.getsize(r["file_path"])
        except OSError:
            continue

    # Estado do banco.
    db_status = "ok"
    try:
        db.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001
        db_status = "error"

    return {
        "users": {
            "total": total_users,
            "free": free_users,
            "pro": pro_users,
            "active": active_users,
            "admins": admins,
        },
        # MRR proxy: contagem de assinantes Pro (1 unidade = 1 Pro).
        "mrr_pro_count": pro_users,
        "paystubs": {"total": total_paystubs},
        "ai": {
            "total_queries": ai_total,
            "queries_24h": ai_24h,
            "queries_1h": ai_1h,
            "blocked_total": blocked_total,
            "model": ai_cost.get("model", ""),
            "input_tokens": ai_cost["input_tokens"],
            "output_tokens": ai_cost["output_tokens"],
            "estimated_tokens": ai_cost["estimated_tokens"],
            "input_cost": ai_cost["input_cost"],
            "output_cost": ai_cost["output_cost"],
            "estimated_cost": ai_cost["estimated_cost"],
            "input_rate_per_million": ai_cost["input_rate_per_million"],
            "output_rate_per_million": ai_cost["output_rate_per_million"],
        },
        "storage": {
            "bytes": storage_bytes,
            "pdf_count": len(pdf_rows),
        },
        "parsing_errors": {"unresolved": unresolved_errors},
        "db": {"status": db_status},
    }


def get_daily_ai_breakdown(db, days: int = 7) -> List[dict]:
    """Volume diário de consultas de IA nos últimos `days` dias."""
    ensure_ai_usage_logs_table(db)
    cutoff = (_now_utc() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    rows = db.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
        FROM ai_usage_logs
        WHERE created_at >= ?
        GROUP BY day
        ORDER BY day ASC
        """,
        [cutoff],
    ).fetchall()
    return [{"date": r["day"], "queries": int(r["c"])} for r in rows]


def get_ai_leaderboard(db, limit: int = 10) -> List[dict]:
    """Usuários com maior consumo de IA (leaderboard de governança)."""
    ensure_ai_usage_logs_table(db)
    rows = db.execute(
        """
        SELECT u.id AS user_id, u.name AS user_name, u.email AS user_email,
               u.plan AS plan, COUNT(l.id) AS queries
        FROM ai_usage_logs l
        JOIN users u ON u.id = l.user_id
        GROUP BY u.id
        ORDER BY queries DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "user_name": r["user_name"],
            "user_email": r["user_email"],
            "plan": r["plan"],
            "queries": int(r["queries"]),
        }
        for r in rows
    ]


def list_blocked_attempts(db, limit: int = 50) -> List[dict]:
    """Log de tentativas de prompt injection rejeitadas (governança)."""
    ensure_ai_blocked_logs_table(db)
    rows = db.execute(
        """
        SELECT b.id, b.user_id, b.reason, b.detail, b.created_at,
               u.name AS user_name, u.email AS user_email
        FROM ai_blocked_logs b
        LEFT JOIN users u ON u.id = b.user_id
        ORDER BY b.created_at DESC, b.id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "user_name": r["user_name"] or "",
            "user_email": r["user_email"] or "",
            "reason": r["reason"],
            "detail": r["detail"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_ai_usage(db) -> dict:
    """Agregação completa de consumo de IA para a aba FinOps."""
    return {
        "daily": get_daily_ai_breakdown(db),
        "leaderboard": get_ai_leaderboard(db),
        "blocked": list_blocked_attempts(db),
    }


def reset_user_usage(db, user_id: int) -> dict:
    """
    Zera o consumo de IA de um usuário (limpa `ai_usage_logs` e
    `ai_blocked_logs`) — usado em chamados de suporte.

    Returns:
        dict: {"user_id", "queries_cleared", "blocked_cleared"}
    """
    ensure_ai_usage_logs_table(db)
    ensure_ai_blocked_logs_table(db)
    queries_cleared = db.execute(
        "DELETE FROM ai_usage_logs WHERE user_id = ?", [user_id]
    ).rowcount
    blocked_cleared = db.execute(
        "DELETE FROM ai_blocked_logs WHERE user_id = ?", [user_id]
    ).rowcount
    db.commit()
    return {
        "user_id": user_id,
        "queries_cleared": queries_cleared,
        "blocked_cleared": blocked_cleared,
    }


def reset_holerites(db, user_id: int) -> dict:
    """
    Apaga todos os holerites (e rubricas associadas) de um usuário — usado em
    chamados de suporte para "Resetar Holerites".

    Returns:
        dict: {"user_id", "holerites_cleared", "rubricas_cleared"}
    """
    rubricas = db.execute(
        "SELECT COUNT(*) AS c FROM rubricas_holerite WHERE user_id = ?",
        [user_id],
    ).fetchone()["c"]
    holerites = db.execute(
        "SELECT COUNT(*) AS c FROM holerites WHERE user_id = ?", [user_id]
    ).fetchone()["c"]
    db.execute("DELETE FROM rubricas_holerite WHERE user_id = ?", [user_id])
    db.execute("DELETE FROM holerites WHERE user_id = ?", [user_id])
    db.commit()
    return {
        "user_id": user_id,
        "holerites_cleared": int(holerites),
        "rubricas_cleared": int(rubricas),
    }


def user_row_with_stats(db, user) -> dict:
    """
    Serializa um usuário com as contagens associadas usadas pelo CRM:
    holerites e consultas de IA nas janelas 24h/1h.
    """
    paystubs = db.execute(
        "SELECT COUNT(*) AS c FROM holerites WHERE user_id = ?", [user.id]
    ).fetchone()["c"]
    ai_24h = count_queries_in_window(db, user.id, 24 * 60 * 60)
    ai_1h = count_queries_in_window(db, user.id, 60 * 60)
    return {
        **user_to_dict(user),
        "paystubs": paystubs,
        "ai_24h": ai_24h,
        "ai_1h": ai_1h,
    }


def list_users_with_stats(db) -> List[dict]:
    """Lista todos os usuários com contagens associadas (CRM)."""
    return [user_row_with_stats(db, u) for u in list_users(db)]


def get_storage_breakdown(db) -> dict:
    """Detalhamento do consumo de armazenamento em disco."""
    try:
        upload_dir = current_app.config["UPLOAD_FOLDER"]
    except RuntimeError:
        upload_dir = ""
    rows = db.execute(
        "SELECT file_path FROM holerites WHERE file_path IS NOT NULL AND TRIM(file_path) != ''"
    ).fetchall()
    files = []
    total = 0
    for r in rows:
        path = r["file_path"]
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        total += size
        files.append({"path": path, "bytes": size})
    # Ordena do maior para o menor.
    files.sort(key=lambda f: f["bytes"], reverse=True)
    return {
        "total_bytes": total,
        "file_count": len(files),
        "upload_dir": upload_dir,
        "files": files[:100],
    }


def clean_orphaned_files(db) -> dict:
    """
    Remove arquivos em `UPLOAD_FOLDER` que não estão referenciados por
    nenhum holerite (arquivos órfãos/temporários).

    Returns:
        dict: {"removed": int, "freed_bytes": int, "orphans": int}
    """
    try:
        upload_dir = current_app.config["UPLOAD_FOLDER"]
    except RuntimeError:
        upload_dir = ""

    referenced = {
        r["file_path"]
        for r in db.execute(
            "SELECT file_path FROM holerites "
            "WHERE file_path IS NOT NULL AND TRIM(file_path) != ''"
        ).fetchall()
    }

    removed = 0
    freed = 0
    if upload_dir and os.path.isdir(upload_dir):
        for name in os.listdir(upload_dir):
            path = os.path.join(upload_dir, name)
            if not os.path.isfile(path):
                continue
            if path in referenced:
                continue
            try:
                freed += os.path.getsize(path)
                os.remove(path)
                removed += 1
            except OSError:
                continue
    return {
        "removed": removed,
        "freed_bytes": freed,
        "orphans": removed,
    }


