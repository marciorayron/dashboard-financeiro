"""
database/queries.py
-------------------
Consultas e migrações de propriedade (data ownership) dos holerites.

Centraliza operações de "quem é o dono do dado" para garantir o isolamento
multi-tenant e permitir a correção de holerites órfãos (dados legados cujo
`user_id` aponta para uma conta que não corresponde ao operador admin).

Funções principais:
  * `claim_holerites_for_admin` -> migração que associa holerites órfãos à
    conta administrativa (`admin@admin.com`), inclusive suas rubricas.
  * `count_holerites`           -> total de holerites de um usuário.
  * `get_admin_user_id`         -> id da conta admin (ou None se inexistente).
"""
from typing import Optional

# E-mail canônico da conta administrativa (dono dos dados neste deployment).
ADMIN_EMAIL = "admin@admin.com"


def get_admin_user_id(db, admin_email: str = ADMIN_EMAIL) -> Optional[int]:
    """Retorna o id da conta admin, ou None se a conta não existir."""
    row = db.execute(
        "SELECT id FROM users WHERE email = ?", [admin_email]
    ).fetchone()
    return row["id"] if row else None


def count_holerites(db, user_id: int) -> int:
    """Total de holerites pertencentes a um usuário."""
    row = db.execute(
        "SELECT COUNT(*) AS c FROM holerites WHERE user_id = ?", [user_id]
    ).fetchone()
    return int(row["c"]) if row else 0


def claim_holerites_for_admin(
    db, admin_email: str = ADMIN_EMAIL
) -> dict:
    """
    Reassocia holerites "órfãos" à conta administrativa (idempotente).

    Executa a migração:
        UPDATE holerites SET user_id = <admin_id>
        WHERE user_id != <admin_id>

    e, para manter a consistência, também reatribui o `user_id` das rubricas
    (`rubricas_holerite`) que pertencem aos holerites agora de propriedade do
    admin. Se a conta admin não existir, nada é alterado (no-op seguro).

    Args:
        db: conexão SQLite ativa.
        admin_email: e-mail da conta administrativa (padrão `admin@admin.com`).

    Returns:
        dict: {"admin_user_id", "moved_holerites", "moved_rubricas"}
    """
    admin_id = get_admin_user_id(db, admin_email)
    if admin_id is None:
        return {"admin_user_id": None, "moved_holerites": 0, "moved_rubricas": 0}

    # 1) Reatribui holerites cujo dono não é o admin.
    moved = db.execute(
        "UPDATE holerites SET user_id = ? WHERE user_id != ?",
        [admin_id, admin_id],
    ).rowcount

    # 2) Reatribui as rubricas dos holerites agora de propriedade do admin.
    moved_rubricas = db.execute(
        """
        UPDATE rubricas_holerite
        SET user_id = ?
        WHERE user_id != ?
          AND holerite_id IN (SELECT id FROM holerites WHERE user_id = ?)
        """,
        [admin_id, admin_id, admin_id],
    ).rowcount

    db.commit()
    return {
        "admin_user_id": admin_id,
        "moved_holerites": moved,
        "moved_rubricas": moved_rubricas,
    }


def assign_holerites_to_user(db, target_user_id: int) -> dict:
    """
    Reatribui TODOS os holerites (e rubricas) a um usuário-alvo (idempotente).

    Permite ao admin "devolver" os holerites de teste a um usuário específico
    (ex.: `marcio`), invertendo a migração padrão para a conta admin.

    Args:
        db: conexão SQLite ativa.
        target_user_id: id do usuário que passará a ser dono dos holerites.

    Returns:
        dict: {"target_user_id", "moved_holerites", "moved_rubricas"}
    """
    exists = db.execute(
        "SELECT id FROM users WHERE id = ?", [target_user_id]
    ).fetchone()
    if exists is None:
        return {
            "target_user_id": None,
            "moved_holerites": 0,
            "moved_rubricas": 0,
        }

    moved = db.execute(
        "UPDATE holerites SET user_id = ? WHERE user_id != ?",
        [target_user_id, target_user_id],
    ).rowcount

    moved_rubricas = db.execute(
        """
        UPDATE rubricas_holerite
        SET user_id = ?
        WHERE user_id != ?
          AND holerite_id IN (SELECT id FROM holerites WHERE user_id = ?)
        """,
        [target_user_id, target_user_id, target_user_id],
    ).rowcount

    db.commit()
    return {
        "target_user_id": target_user_id,
        "moved_holerites": moved,
        "moved_rubricas": moved_rubricas,
    }
