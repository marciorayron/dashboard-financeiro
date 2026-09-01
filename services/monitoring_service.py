"""
services/monitoring_service.py
-------------------------------
Monitoramento e auditoria do sistema para o painel administrativo (`/admin`).

Responsabilidades:
  * Tabela `parse_errors`: log persistente de erros de parsing de PDFs, com
    contexto do usuário (tenant), nome do arquivo, timestamp e detalhes da
    exceção (tipo, mensagem e traceback) — para o admin reanalisar/corrigir
    uploads quebrados.
  * `get_system_health`: métricas globais de saúde do sistema — estado da
    conexão com o banco, total de registros, usuários ativos, uso de
    armazenamento (PDFs) e contagem de uploads.
"""
import os
from typing import List, Optional

from flask import current_app

# Nome da tabela de auditoria de erros de parsing.
PARSE_ERRORS_TABLE = "parse_errors"


def ensure_parse_errors_table(db) -> None:
    """Garante a existência da tabela `parse_errors` (idempotente)."""
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PARSE_ERRORS_TABLE} (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            filename       TEXT,
            error_type     TEXT,
            error_message  TEXT,
            traceback      TEXT,
            resolved       INTEGER NOT NULL DEFAULT 0
                           CHECK (resolved IN (0, 1)),
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            resolved_at    TEXT
        )
        """
    )
    db.commit()


def record_parse_error(
    db,
    user_id: int,
    filename: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    traceback: Optional[str] = None,
) -> int:
    """
    Registra um erro de parsing de PDF associado a um usuário (tenant).

    Returns:
        int: id do registro criado.
    """
    ensure_parse_errors_table(db)
    cur = db.execute(
        f"""
        INSERT INTO {PARSE_ERRORS_TABLE}
            (user_id, filename, error_type, error_message, traceback)
        VALUES (?, ?, ?, ?, ?)
        """,
        [user_id, filename, error_type, error_message, traceback],
    )
    db.commit()
    return cur.lastrowid


def _row_to_dict(row) -> dict:
    """Serializa uma linha de erro com o nome do usuário (contexto)."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "user_name": row["user_name"] or "",
        "user_email": row["user_email"] or "",
        "filename": row["filename"],
        "error_type": row["error_type"],
        "error_message": row["error_message"],
        "traceback": row["traceback"],
        "resolved": bool(row["resolved"]),
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def list_parse_errors(
    db, only_unresolved: bool = True, limit: int = 100
) -> List[dict]:
    """
    Lista os erros de parsing, enriquecidos com o contexto do usuário.

    Args:
        only_unresolved: True (padrão) retorna apenas erros não resolvidos.
        limit: máximo de registros retornados (mais recentes primeiro).
    """
    ensure_parse_errors_table(db)
    where = "WHERE pe.resolved = 0" if only_unresolved else ""
    rows = db.execute(
        f"""
        SELECT pe.*, u.name AS user_name, u.email AS user_email
        FROM {PARSE_ERRORS_TABLE} pe
        LEFT JOIN users u ON u.id = pe.user_id
        {where}
        ORDER BY pe.created_at DESC, pe.id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_unresolved_parse_errors(db) -> int:
    """Total de erros de parsing ainda não resolvidos."""
    ensure_parse_errors_table(db)
    row = db.execute(
        f"SELECT COUNT(*) AS c FROM {PARSE_ERRORS_TABLE} WHERE resolved = 0"
    ).fetchone()
    return int(row["c"]) if row else 0


def mark_parse_error_resolved(db, error_id: int) -> bool:
    """Marca um erro de parsing como resolvido. Retorna True se existia."""
    ensure_parse_errors_table(db)
    cur = db.execute(
        f"""
        UPDATE {PARSE_ERRORS_TABLE}
        SET resolved = 1, resolved_at = datetime('now')
        WHERE id = ?
        """,
        [error_id],
    )
    db.commit()
    return cur.rowcount > 0


def get_system_health(db) -> dict:
    """
    Métricas globais de saúde do sistema para o painel administrativo.

    Retorna estado da conexão, contagens, usuários ativos e uso de
    armazenamento (quantidade de PDFs e bytes totais em disco).
    """
    health = {"db": {"status": "ok", "engine": "sqlite"}}

    try:
        # Prova de leitura na conexão atual (confirma que o banco responde).
        db.execute("SELECT 1").fetchone()
    except Exception:  # noqa: BLE001
        health["db"] = {"status": "error", "engine": "sqlite"}

    health["total_users"] = db.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]
    health["active_users"] = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_active = 1"
    ).fetchone()["c"]
    health["admins"] = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
    ).fetchone()["c"]
    health["total_paystubs"] = db.execute(
        "SELECT COUNT(*) AS c FROM holerites"
    ).fetchone()["c"]
    health["unresolved_parsing_errors"] = count_unresolved_parse_errors(db)

    # Armazenamento: arquivos PDF persistidos e uso total em disco.
    pdf_rows = db.execute(
        "SELECT file_path FROM holerites WHERE file_path IS NOT NULL AND TRIM(file_path) != ''"
    ).fetchall()
    health["pdf_uploads"] = len(pdf_rows)
    total_bytes = 0
    for r in pdf_rows:
        try:
            if os.path.exists(r["file_path"]):
                total_bytes += os.path.getsize(r["file_path"])
        except OSError:
            continue
    health["storage_bytes"] = total_bytes

    # Conectividade declarada pela config (para exibição).
    try:
        health["db"]["path"] = str(current_app.config.get("DATABASE_PATH", ""))
    except RuntimeError:  # fora de app context
        health["db"]["path"] = ""

    return health
