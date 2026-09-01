"""
database/seed.py
----------------
Seeder automático do administrador padrão (RBAC).

Provisiona a conta `admin` na primeira execução — quando nenhum usuário com
papel 'admin' existe — lendo as credenciais de variáveis de ambiente
(`ADMIN_EMAIL` / `ADMIN_PASSWORD`). Também expõe um modo `force` para
re-provisionar/resetar a conta via CLI (`flask seed-admin --reset`).

As credenciais padrão devem ser trocadas em produção definindo as variáveis
de ambiente (nunca usar `admin@admin.com`/`admin123` fora de dev).
"""
import logging
import os

from werkzeug.security import generate_password_hash

from models.user import ROLE_ADMIN, USER_TABLE

logger = logging.getLogger(__name__)

# Credenciais padrão (substituíveis por variáveis de ambiente).
DEFAULT_ADMIN_EMAIL = "admin@admin.com"
DEFAULT_ADMIN_PASSWORD = "admin123"
# Nome de exibição da conta administrativa padrão.
DEFAULT_ADMIN_NAME = "Administrador"


def seed_default_admin(db, force: bool = False) -> dict:
    """
    Garante a existência de ao menos um usuário com papel 'admin'.

    Se já existir um admin e `force` for False, não faz nada. Caso contrário,
    cria a conta padrão a partir das variáveis de ambiente — ou, se a conta
    com o e-mail padrão já existir, promove-a a `admin` e reseta a senha
    (evita violar o UNIQUE em `users.email`).

    Args:
        db: conexão SQLite ativa.
        force: quando True, re-provisiona/reseta o admin padrão mesmo que já
               exista um admin.

    Returns:
        dict: {"created": bool, "reset": bool, "skipped": bool, "email": str}
    """
    email = (
        os.getenv("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL).strip().lower()
        or DEFAULT_ADMIN_EMAIL
    )
    password = os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD) or DEFAULT_ADMIN_PASSWORD

    existing_admin = db.execute(
        f"SELECT COUNT(*) AS c FROM {USER_TABLE} WHERE role = ?", [ROLE_ADMIN]
    ).fetchone()["c"]

    if existing_admin and not force:
        return {"created": False, "reset": False, "skipped": True, "email": email}

    # Localiza a conta pelo e-mail para decidir entre criar ou promover/resetar.
    row = db.execute(
        f"SELECT id FROM {USER_TABLE} WHERE email = ?", [email]
    ).fetchone()

    password_hash = generate_password_hash(password)
    reset = row is not None

    if reset:
        db.execute(
            f"""
            UPDATE {USER_TABLE}
            SET role = ?, password_hash = ?, is_active = 1, updated_at = datetime('now')
            WHERE email = ?
            """,
            [ROLE_ADMIN, password_hash, email],
        )
    else:
        db.execute(
            f"""
            INSERT INTO {USER_TABLE} (name, email, password_hash, role, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            [DEFAULT_ADMIN_NAME, email, password_hash, ROLE_ADMIN],
        )
    db.commit()

    action = "reset" if reset else "created"
    logger.info("[SEED] Default admin user %s: %s", action, email)
    return {"created": not reset, "reset": reset, "skipped": False, "email": email}
