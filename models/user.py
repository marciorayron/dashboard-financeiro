"""
models/user.py
--------------
Modelo de usuário e funções de RBAC (Role-Based Access Control).

A aplicação é multi-tenant: cada linha em `users` é uma conta independente,
e todas as tabelas core são escopadas por `user_id`. Este módulo centraliza:

  * O modelo `User` (dataclass) com a coluna `role` ('admin' | 'user').
  * Migração do schema para adicionar/garantir a coluna `role`.
  * Helpers de consulta e gestão de usuários usados pelo painel `/admin`
    (ativar/desativar, trocar papel, resetar senha).

Papéis disponíveis:
  * 'admin' -> acesso ao painel administrativo (`/admin`).
  * 'user'  -> acesso apenas ao dashboard pessoal (padrão).
"""
import secrets
from dataclasses import asdict, dataclass
from typing import List, Optional

from werkzeug.security import generate_password_hash

# Papéis suportados e valores canônicos.
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

# Planos de assinatura (Freemium).
PLAN_FREE = "free"
PLAN_PRO = "pro"
PLANS = (PLAN_FREE, PLAN_PRO)

# Nome da tabela de persistência.
USER_TABLE = "users"


@dataclass
class User:
    """Uma conta da aplicação (tenant)."""

    id: Optional[int] = None
    name: str = ""
    email: str = ""
    role: str = ROLE_USER
    plan: str = PLAN_FREE
    is_active: bool = True
    password_hash: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_pro(self) -> bool:
        return self.plan == PLAN_PRO


def _row_to_user(row) -> User:
    """Converte uma linha SQLite em um objeto User."""
    if row is None:
        return None
    return User(
        id=row["id"],
        name=row["name"],
        email=row["email"],
        role=str(row["role"] or ROLE_USER).lower(),
        plan=str(row["plan"] or PLAN_FREE).lower(),
        is_active=bool(row["is_active"]),
        password_hash=row["password_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_user_table(db):
    """
    Garante a existência da tabela `users` com as colunas `role` e `plan`
    (idempotente).

    Destinado a bancos novos; bancos já existentes são migrados por
    `ensure_user_role_column` e `ensure_user_plan_column`.
    """
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {USER_TABLE} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            email         TEXT    NOT NULL UNIQUE,
            password_hash TEXT,
            role          TEXT    NOT NULL DEFAULT 'user'
                                   CHECK (role IN ('admin', 'user')),
            plan          TEXT    NOT NULL DEFAULT 'free'
                                   CHECK (plan IN ('free', 'pro')),
            is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    ensure_user_role_column(db)
    ensure_user_plan_column(db)
    db.commit()


def ensure_user_role_column(db):
    """
    Adiciona a coluna `role` a bancos criados antes da migração de RBAC.

    Idempotente: só altera a tabela se a coluna ainda não existir.
    """
    cols = {
        r["name"]
        for r in db.execute(f"PRAGMA table_info({USER_TABLE})").fetchall()
    }
    if "role" not in cols:
        db.execute(
            f"ALTER TABLE {USER_TABLE} ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
        )
    # Defensivo: normaliza valores inválidos para 'user'.
    db.execute(
        f"""
        UPDATE {USER_TABLE}
        SET role = 'user'
        WHERE role NOT IN ('admin', 'user') OR role IS NULL
        """
    )


def ensure_user_plan_column(db):
    """
    Adiciona a coluna `plan` ('free' | 'pro') a bancos criados antes da
    migração Freemium.

    Idempotente: só altera a tabela se a coluna ainda não existir. Todos os
    usuários existentes iniciam no plano Gratuito.
    """
    cols = {
        r["name"]
        for r in db.execute(f"PRAGMA table_info({USER_TABLE})").fetchall()
    }
    if "plan" not in cols:
        db.execute(
            f"ALTER TABLE {USER_TABLE} ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'"
        )
    # Defensivo: normaliza valores inválidos para 'free'.
    db.execute(
        f"""
        UPDATE {USER_TABLE}
        SET plan = 'free'
        WHERE plan NOT IN ('free', 'pro') OR plan IS NULL
        """
    )


def get_user(db, user_id: int) -> Optional[User]:
    """Retorna um usuário pelo id, ou None se não existir."""
    row = db.execute(
        f"SELECT * FROM {USER_TABLE} WHERE id = ?", [user_id]
    ).fetchone()
    return _row_to_user(row)


def get_user_by_email(db, email: str) -> Optional[User]:
    """Retorna um usuário pelo e-mail (único), ou None."""
    row = db.execute(
        f"SELECT * FROM {USER_TABLE} WHERE email = ?", [email]
    ).fetchone()
    return _row_to_user(row)


def list_users(db) -> List[User]:
    """Lista todos os usuários, ordenados por data de criação."""
    rows = db.execute(
        f"SELECT * FROM {USER_TABLE} ORDER BY created_at ASC, id ASC"
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def is_admin(db, user_id: int) -> bool:
    """True se o usuário for admin. Usuário inexistente nunca é admin."""
    if not user_id:
        return False
    row = db.execute(
        f"SELECT role FROM {USER_TABLE} WHERE id = ?", [user_id]
    ).fetchone()
    return bool(row) and str(row["role"] or "").lower() == ROLE_ADMIN


def set_user_role(db, user_id: int, role: str) -> Optional[User]:
    """Define o papel de um usuário ('admin' | 'user')."""
    role = str(role or "").lower().strip()
    if role not in ROLES:
        role = ROLE_USER
    db.execute(
        f"UPDATE {USER_TABLE} SET role = ?, updated_at = datetime('now') WHERE id = ?",
        [role, user_id],
    )
    db.commit()
    return get_user(db, user_id)


def set_user_plan(db, user_id: int, plan: str) -> Optional[User]:
    """Define o plano de assinatura de um usuário ('free' | 'pro')."""
    plan = str(plan or "").lower().strip()
    if plan not in PLANS:
        plan = PLAN_FREE
    db.execute(
        f"UPDATE {USER_TABLE} SET plan = ?, updated_at = datetime('now') WHERE id = ?",
        [plan, user_id],
    )
    db.commit()
    return get_user(db, user_id)


def set_user_active(db, user_id: int, active: bool) -> Optional[User]:
    """Ativa/desativa uma conta. Retorna o usuário atualizado (ou None)."""
    db.execute(
        f"UPDATE {USER_TABLE} SET is_active = ?, updated_at = datetime('now') WHERE id = ?",
        [1 if active else 0, user_id],
    )
    db.commit()
    return get_user(db, user_id)


def reset_user_password(db, user_id: int) -> str:
    """
    Gera uma senha temporária aleatória para o usuário e a grava com hash.

    Returns:
        str: a senha temporária em texto puro (exibida UMA vez ao admin).
    """
    temporary = secrets.token_urlsafe(10)
    db.execute(
        f"UPDATE {USER_TABLE} SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
        [generate_password_hash(temporary), user_id],
    )
    db.commit()
    return temporary


def create_user(
    db,
    name: str,
    email: str,
    password: str,
    role: str = ROLE_USER,
    plan: str = PLAN_FREE,
) -> User:
    """
    Cria um novo usuário (operacional, a partir do painel admin).

    Args:
        name (str): nome de exibição.
        email (str): e-mail único (normalizado para minúsculas).
        password (str): senha em texto puro (gravada com hash).
        role (str): 'admin' | 'user'.
        plan (str): 'free' | 'pro'.

    Raises:
        ValueError: quando os campos são inválidos ou o e-mail já existe.

    Returns:
        User: o usuário recém-criado.
    """
    name = (name or "").strip()
    email = (email or "").strip().lower()
    password = str(password or "").strip()
    role = str(role or "").lower().strip()
    plan = str(plan or "").lower().strip()

    if role not in ROLES:
        role = ROLE_USER
    if plan not in PLANS:
        plan = PLAN_FREE
    if not name or not email or not password:
        raise ValueError("Nome, e-mail e senha são obrigatórios.")
    if len(password) < 6:
        raise ValueError("A senha deve ter ao menos 6 caracteres.")
    exists = db.execute(
        f"SELECT id FROM {USER_TABLE} WHERE email = ?", [email]
    ).fetchone()
    if exists:
        raise ValueError("Já existe um usuário com este e-mail.")

    cur = db.execute(
        f"""
        INSERT INTO {USER_TABLE}
            (name, email, password_hash, role, plan, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        [name, email, generate_password_hash(password), role, plan],
    )
    db.commit()
    return get_user(db, cur.lastrowid)


def set_user_password(db, user_id: int, password: str) -> Optional[User]:
    """
    Define uma senha explícita para o usuário (reset direcionado).

    Raises:
        ValueError: quando a senha é muito curta.
    """
    password = str(password or "").strip()
    if len(password) < 6:
        raise ValueError("A senha deve ter ao menos 6 caracteres.")
    db.execute(
        f"UPDATE {USER_TABLE} SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
        [generate_password_hash(password), user_id],
    )
    db.commit()
    return get_user(db, user_id)


def delete_user(db, user_id: int) -> bool:
    """
    Exclui um usuário da base (cascade remove dados associados).

    Returns:
        bool: True se algum usuário foi removido.
    """
    cur = db.execute(
        f"DELETE FROM {USER_TABLE} WHERE id = ?", [user_id]
    )
    db.commit()
    return cur.rowcount > 0


def to_dict(user: User) -> dict:
    """Serializa um usuário para JSON (sem o hash de senha)."""
    data = asdict(user)
    data.pop("password_hash", None)
    return data
