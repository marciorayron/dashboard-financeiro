"""
models/profile.py
-----------------
Modelo de perfil do usuário que impulsiona métricas financeiras
personalizadas (salário líquido recorrente teórico, etc.).

Persistido em uma linha por usuário na tabela `user_profiles`.
"""
from dataclasses import asdict, dataclass
from datetime import date
from typing import List, Optional

# Tipos de contrato aceitos.
CONTRACT_TYPES = ("HORISTA", "MENSALISTA")

# Nome da tabela de persistência.
PROFILE_TABLE = "user_profiles"

# Tabela de histórico de revisões do perfil (versionamento de contrato).
HISTORY_TABLE = "user_profile_history"

# Campos aceitos ao salvar o perfil via API.
PROFILE_FIELDS = (
    "admission_date",
    "job_title",
    "contract_type",
    "base_rate",
    "monthly_hours",
    "irrf_dependents",
    "fixed_benefits_deduction",
    "overtime_tier1_rate",
    "overtime_tier1_limit",
    "overtime_tier2_rate",
)

# Colunas extras adicionadas em migrações posteriores (ALTER TABLE).
_EXTRA_COLUMNS = [
    ("overtime_tier1_rate", "REAL NOT NULL DEFAULT 1.70"),
    ("overtime_tier1_limit", "REAL NOT NULL DEFAULT 30"),
    ("overtime_tier2_rate", "REAL NOT NULL DEFAULT 2.00"),
]


def _f(value) -> float:
    """Converte um valor arbitrário em float com fallback seguro."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class UserProfile:
    """Dados de contratação/benefícios do usuário."""

    user_id: Optional[int] = None
    admission_date: Optional[str] = None      # 'YYYY-MM-DD'
    job_title: Optional[str] = None
    contract_type: str = "HORISTA"            # 'HORISTA' | 'MENSALISTA'
    base_rate: float = 0.0                    # valor por hora ou salário mensal
    monthly_hours: float = 220.0
    irrf_dependents: int = 0
    fixed_benefits_deduction: float = 0.0
    # Regras progressivas de horas extras.
    overtime_tier1_rate: float = 1.70          # multiplicador até o limite
    overtime_tier1_limit: float = 30.0         # horas no 1º patamar
    overtime_tier2_rate: float = 2.00          # multiplicador acima do limite

    @property
    def label_rate(self) -> str:
        """Rótulo dinâmico do campo de valor."""
        return "Valor por Hora (R$)" if self.contract_type == "HORISTA" else "Salário Base Mensal (R$)"

    @property
    def multipliers_label(self) -> str:
        """Ex.: '70% até 30h | 100% acima'."""
        p1 = int(round((self.overtime_tier1_rate - 1) * 100))
        p2 = int(round((self.overtime_tier2_rate - 1) * 100))
        lim = int(self.overtime_tier1_limit or 0)
        return f"{p1}% até {lim}h | {p2}% acima"


@dataclass
class UserProfileHistory:
    """
    Snapshot versionado dos termos de contratação de um usuário.

    Cada linha representa o perfil vigente a partir de `effective_date`.
    Usado para resolver a taxa de pagamento ativa na data de cada paystub.
    """

    id: Optional[int] = None
    user_id: Optional[int] = None
    effective_date: Optional[str] = None    # 'YYYY-MM-DD'
    job_title: Optional[str] = None
    contract_type: str = "HORISTA"
    base_rate: float = 0.0
    monthly_hours: float = 220.0
    irrf_dependents: int = 0
    fixed_benefits_deduction: float = 0.0
    created_at: Optional[str] = None


def create_user_profile_history_table(db):
    """
    Garante a existência da tabela `user_profile_history` (idempotente).

    A criação também está no `schema.sql`; este helper cobre bancos já
    existentes que não receberam a tabela no momento da criação.
    """
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                  INTEGER NOT NULL
                                     REFERENCES users(id) ON DELETE CASCADE,
            effective_date           TEXT NOT NULL DEFAULT (date('now')),
            job_title                TEXT,
            contract_type            TEXT NOT NULL DEFAULT 'HORISTA',
            base_rate                REAL NOT NULL DEFAULT 0.0,
            monthly_hours            REAL NOT NULL DEFAULT 220.0,
            irrf_dependents          INTEGER NOT NULL DEFAULT 0,
            fixed_benefits_deduction REAL NOT NULL DEFAULT 0.0,
            created_at               TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_profile_history_user_date
            ON {HISTORY_TABLE} (user_id, effective_date)
        """
    )
    db.commit()


def _ensure_columns(db):
    """Adiciona colunas que porventura não existam (migração de schema)."""
    cols = {
        r["name"]
        for r in db.execute(f"PRAGMA table_info({PROFILE_TABLE})").fetchall()
    }
    for name, decl in _EXTRA_COLUMNS:
        if name not in cols:
            db.execute(f"ALTER TABLE {PROFILE_TABLE} ADD COLUMN {name} {decl}")


def create_profiles_table(db):
    """
    Garante a existência da tabela `user_profiles` (idempotente) e aplica
    migrações de colunas novas em tabelas já existentes.
    """
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PROFILE_TABLE} (
            user_id                  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            admission_date           TEXT,
            job_title                TEXT,
            contract_type            TEXT NOT NULL DEFAULT 'HORISTA'
                                     CHECK (contract_type IN ('HORISTA', 'MENSALISTA')),
            base_rate                REAL NOT NULL DEFAULT 0.0,
            monthly_hours            REAL NOT NULL DEFAULT 220.0,
            irrf_dependents          INTEGER NOT NULL DEFAULT 0,
            fixed_benefits_deduction REAL NOT NULL DEFAULT 0.0,
            overtime_tier1_rate      REAL NOT NULL DEFAULT 1.70,
            overtime_tier1_limit     REAL NOT NULL DEFAULT 30,
            overtime_tier2_rate      REAL NOT NULL DEFAULT 2.00,
            updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    _ensure_columns(db)
    db.commit()


def load_profile(db, user_id: int) -> UserProfile:
    """Carrega o perfil de um usuário; devolve um perfil padrão se inexistente."""
    create_profiles_table(db)
    row = db.execute(
        f"SELECT * FROM {PROFILE_TABLE} WHERE user_id = ?", [user_id]
    ).fetchone()
    if row is None:
        return UserProfile(user_id=user_id)
    return UserProfile(
        user_id=row["user_id"],
        admission_date=row["admission_date"],
        job_title=row["job_title"],
        contract_type=str(row["contract_type"] or "HORISTA").upper(),
        base_rate=_f(row["base_rate"]),
        monthly_hours=_f(row["monthly_hours"]) or 220.0,
        irrf_dependents=int(row["irrf_dependents"] or 0),
        fixed_benefits_deduction=_f(row["fixed_benefits_deduction"]),
        overtime_tier1_rate=_f(row["overtime_tier1_rate"]) or 1.70,
        overtime_tier1_limit=_f(row["overtime_tier1_limit"]) or 30.0,
        overtime_tier2_rate=_f(row["overtime_tier2_rate"]) or 2.00,
    )


def save_profile(db, user_id: int, data: dict) -> UserProfile:
    """Persiste (upsert) o perfil do usuário e devolve o modelo salvo."""
    create_profiles_table(db)
    create_user_profile_history_table(db)
    contract = str(data.get("contract_type") or "HORISTA").upper()
    if contract not in CONTRACT_TYPES:
        contract = "HORISTA"

    profile = UserProfile(
        user_id=user_id,
        admission_date=(data.get("admission_date") or None),
        job_title=(data.get("job_title") or None),
        contract_type=contract,
        base_rate=_f(data.get("base_rate")),
        monthly_hours=_f(data.get("monthly_hours")) or 220.0,
        irrf_dependents=int(data.get("irrf_dependents") or 0),
        fixed_benefits_deduction=_f(data.get("fixed_benefits_deduction")),
        overtime_tier1_rate=_f(data.get("overtime_tier1_rate")) or 1.70,
        overtime_tier1_limit=_f(data.get("overtime_tier1_limit")) or 30.0,
        overtime_tier2_rate=_f(data.get("overtime_tier2_rate")) or 2.00,
    )

    # ---- Versionamento: grava snapshot quando os termos de contrato mudam ----
    current = load_profile(db, user_id)
    is_new = not _has_contract_data(current)
    terms_changed = _contract_terms_changed(current, profile)

    if is_new or terms_changed:
        # Perfil novo -> vigência a partir da data de admissão (ou hoje).
        # Alteração de termos -> vigência a partir de hoje.
        effective = profile.admission_date if is_new and profile.admission_date else date.today().isoformat()
        _record_history_snapshot(db, profile, effective)

    db.execute(
        f"""
        INSERT INTO {PROFILE_TABLE}
            (user_id, admission_date, job_title, contract_type, base_rate,
             monthly_hours, irrf_dependents, fixed_benefits_deduction,
             overtime_tier1_rate, overtime_tier1_limit, overtime_tier2_rate,
             updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            admission_date = excluded.admission_date,
            job_title = excluded.job_title,
            contract_type = excluded.contract_type,
            base_rate = excluded.base_rate,
            monthly_hours = excluded.monthly_hours,
            irrf_dependents = excluded.irrf_dependents,
            fixed_benefits_deduction = excluded.fixed_benefits_deduction,
            overtime_tier1_rate = excluded.overtime_tier1_rate,
            overtime_tier1_limit = excluded.overtime_tier1_limit,
            overtime_tier2_rate = excluded.overtime_tier2_rate,
            updated_at = datetime('now')
        """,
        [
            profile.user_id,
            profile.admission_date,
            profile.job_title,
            profile.contract_type,
            profile.base_rate,
            profile.monthly_hours,
            profile.irrf_dependents,
            profile.fixed_benefits_deduction,
            profile.overtime_tier1_rate,
            profile.overtime_tier1_limit,
            profile.overtime_tier2_rate,
        ],
    )
    db.commit()
    return profile


def _has_contract_data(profile: UserProfile) -> bool:
    """
    Indica se um perfil já possui dados de contrato efetivos.

    Um perfil "vazio" (recém-inicializado) tem base_rate 0, sem cargo,
    contrato default 'HORISTA' e sem data de admissão.
    """
    if profile is None:
        return False
    if _f(profile.base_rate) > 0:
        return True
    if profile.job_title:
        return True
    if profile.admission_date:
        return True
    return False


def _contract_terms_changed(current: UserProfile, new: UserProfile) -> bool:
    """
    Compara os termos de contrato que importam para a vigência do salário:
    valor da taxa, tipo de contrato e cargo.
    """
    if current is None:
        return True
    rate_changed = _f(current.base_rate) != _f(new.base_rate)
    type_changed = (current.contract_type or "HORISTA").upper() != (new.contract_type or "HORISTA").upper()
    title_changed = (current.job_title or "") != (new.job_title or "")
    return rate_changed or type_changed or title_changed


def _record_history_snapshot(db, profile: UserProfile, effective_date: str) -> int:
    """
    Insere um snapshot de `profile` na tabela de histórico e devolve o id.

    O snapshot captura os termos de contrato completos a partir de
    `effective_date` (YYYY-MM-DD). O commit é feito junto com a operação
    principal em `save_profile`.
    """
    db.execute(
        f"""
        INSERT INTO {HISTORY_TABLE}
            (user_id, effective_date, job_title, contract_type, base_rate,
             monthly_hours, irrf_dependents, fixed_benefits_deduction)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            profile.user_id,
            effective_date,
            profile.job_title,
            profile.contract_type,
            _f(profile.base_rate),
            _f(profile.monthly_hours) or 220.0,
            int(profile.irrf_dependents or 0),
            _f(profile.fixed_benefits_deduction),
        ],
    )
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def load_profile_history(db, user_id: int) -> List[UserProfileHistory]:
    """Lista o histórico de revisões do perfil (mais recente primeiro)."""
    create_user_profile_history_table(db)
    rows = db.execute(
        f"""
        SELECT * FROM {HISTORY_TABLE}
        WHERE user_id = ?
        ORDER BY effective_date ASC, id ASC
        """,
        [user_id],
    ).fetchall()
    return [
        UserProfileHistory(
            id=row["id"],
            user_id=row["user_id"],
            effective_date=row["effective_date"],
            job_title=row["job_title"],
            contract_type=str(row["contract_type"] or "HORISTA").upper(),
            base_rate=_f(row["base_rate"]),
            monthly_hours=_f(row["monthly_hours"]) or 220.0,
            irrf_dependents=int(row["irrf_dependents"] or 0),
            fixed_benefits_deduction=_f(row["fixed_benefits_deduction"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def resolve_profile_for_date(db, user_id: int, date_str: str) -> Optional[UserProfile]:
    """
    Resolve o perfil (e, em especial, a taxa de pagamento) vigente na data.

    Retorna o snapshot histórico mais recente com `effective_date <= date_str`.
    Se nenhum snapshot histórico cobrir a data, retorna o perfil atual como
    fallback (para perfis criados antes da migração de versionamento).
    """
    create_user_profile_history_table(db)
    row = db.execute(
        f"""
        SELECT * FROM {HISTORY_TABLE}
        WHERE user_id = ? AND effective_date <= ?
        ORDER BY effective_date DESC, id DESC
        LIMIT 1
        """,
        [user_id, date_str],
    ).fetchone()
    if row is None:
        return load_profile(db, user_id)
    return UserProfile(
        user_id=user_id,
        admission_date=row["effective_date"],
        job_title=row["job_title"],
        contract_type=str(row["contract_type"] or "HORISTA").upper(),
        base_rate=_f(row["base_rate"]),
        monthly_hours=_f(row["monthly_hours"]) or 220.0,
        irrf_dependents=int(row["irrf_dependents"] or 0),
        fixed_benefits_deduction=_f(row["fixed_benefits_deduction"]),
    )


def to_dict(profile: UserProfile) -> dict:
    """Serializa o perfil para JSON."""
    return asdict(profile)
