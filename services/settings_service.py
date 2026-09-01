"""
services/settings_service.py
-----------------------------
Persistência e leitura de configurações globais editáveis pelo painel
administrativo (`/admin`), sem necessidade de novo deploy.

Cobertura:
  * Faixas progressivas de INSS e IRRF (alíquotas e deduções) + dedução por
    dependente — consumidas pelo `analytics_service` em tempo de execução.
  * Catálogo oficial de rubricas (`catalogo_rubricas`): códigos padrão de
    proventos/descontos que guiam a classificação determinística dos holerites.

As faixas são armazenadas em JSON na tabela `app_settings`. Quando não há
configuração salva, são usados os valores padrão (referentes à legislação 2025).
"""
import json

# Nome da tabela de configurações globais.
SETTINGS_TABLE = "app_settings"

# Faixas padrão (legislação 2025) — usadas quando não há override no banco.
# Formato de cada faixa: (teto_da_faixa, aliquota, parcela_a_deduzir).
DEFAULT_INSS_BRACKETS = [
    [1518.00, 0.075, 0.00],
    [2793.88, 0.090, 113.85],
    [4190.83, 0.120, 228.68],
    [8157.41, 0.140, 396.31],
]
DEFAULT_IRRF_BRACKETS = [
    [2259.20, 0.000, 0.00],
    [2826.65, 0.075, 169.44],
    [3751.05, 0.150, 381.44],
    [4664.68, 0.225, 662.77],
    [float("inf"), 0.275, 896.00],
]
# Dedução por dependente no IRRF.
DEFAULT_DEPENDENT_DEDUCTION = 189.59

# Chaves das configurações na tabela `app_settings`.
KEY_INSS = "tax_inss_brackets"
KEY_IRRF = "tax_irrf_brackets"
KEY_DEPENDENT = "tax_dependent_deduction"


def ensure_settings_table(db):
    """Garante a existência da tabela `app_settings` (idempotente)."""
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SETTINGS_TABLE} (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.commit()


def get_settings(db, key: str, default=None):
    """
    Lê uma configuração (JSON) da tabela `app_settings`.

    Retorna `default` se a chave não existir ou o JSON for inválido.
    """
    ensure_settings_table(db)
    row = db.execute(
        f"SELECT value FROM {SETTINGS_TABLE} WHERE key = ?", [key]
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return default


def set_settings(db, key: str, value) -> None:
    """Grava uma configuração (serializada em JSON) de forma upsert."""
    ensure_settings_table(db)
    db.execute(
        f"""
        INSERT INTO {SETTINGS_TABLE} (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = datetime('now')
        """,
        [key, json.dumps(value, ensure_ascii=False)],
    )
    db.commit()


def get_tax_settings(db) -> dict:
    """
    Retorna as faixas de INSS/IRRF e a dedução por dependente vigentes.

    Usa os valores salvos no `app_settings` quando existirem; caso contrário
    recai sobre os padrões da legislação 2025.
    """
    inss = get_settings(db, KEY_INSS, None) or DEFAULT_INSS_BRACKETS
    irrf = get_settings(db, KEY_IRRF, None) or DEFAULT_IRRF_BRACKETS
    dependent = get_settings(db, KEY_DEPENDENT, None) or DEFAULT_DEPENDENT_DEDUCTION
    return {
        "inss": [[float(b[0]), float(b[1]), float(b[2])] for b in inss],
        "irrf": [[float(b[0]), float(b[1]), float(b[2])] for b in irrf],
        "dependent_deduction": float(dependent),
    }

def save_tax_settings(db, data: dict) -> dict:
    """
    Persiste faixas de INSS/IRRF e dedução por dependente enviadas pelo admin.

    Args:
        data (dict): {"inss": [[...]], "irrf": [[...]], "dependent_deduction": float}

    Returns:
        dict: o estado salvo (via `get_tax_settings`).
    """
    data = data or {}

    def _clean_brackets(raw, default):
        if not isinstance(raw, list) or not raw:
            return default
        cleaned = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            cleaned.append([float(item[0]), float(item[1]), float(item[2])])
        return cleaned or default

    inss = _clean_brackets(data.get("inss"), DEFAULT_INSS_BRACKETS)
    irrf = _clean_brackets(data.get("irrf"), DEFAULT_IRRF_BRACKETS)
    try:
        dependent = float(data.get("dependent_deduction") or DEFAULT_DEPENDENT_DEDUCTION)
    except (TypeError, ValueError):
        dependent = DEFAULT_DEPENDENT_DEDUCTION

    set_settings(db, KEY_INSS, inss)
    set_settings(db, KEY_IRRF, irrf)
    set_settings(db, KEY_DEPENDENT, dependent)
    return get_tax_settings(db)


def list_catalog(db) -> list:
    """Lista o catálogo oficial de rubricas (códigos padrão de classificação)."""
    ensure_settings_table(db)
    rows = db.execute(
        "SELECT codigo, descricao, tipo FROM catalogo_rubricas "
        "ORDER BY tipo, codigo"
    ).fetchall()
    return [
        {
            "codigo": r["codigo"],
            "descricao": r["descricao"],
            "tipo": str(r["tipo"]).upper(),
        }
        for r in rows
    ]


def upsert_catalog(db, codigo: str, descricao: str, tipo: str) -> dict:
    """Insere/atualiza um código padrão de rubrica no catálogo."""
    codigo = str(codigo or "").strip().upper()
    tipo = str(tipo or "").strip().upper()
    if tipo not in ("PROVENTO", "DESCONTO"):
        raise ValueError("tipo deve ser 'PROVENTO' ou 'DESCONTO'.")
    if not codigo:
        raise ValueError("código é obrigatório.")
    db.execute(
        """
        INSERT INTO catalogo_rubricas (codigo, descricao, tipo)
        VALUES (?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET
            descricao = excluded.descricao,
            tipo = excluded.tipo
        """,
        [codigo, str(descricao or ""), tipo],
    )
    db.commit()
    return {"codigo": codigo, "descricao": str(descricao or ""), "tipo": tipo}


def delete_catalog(db, codigo: str) -> bool:
    """Remove um código do catálogo. Retorna True se algo foi removido."""
    codigo = str(codigo or "").strip().upper()
    cur = db.execute(
        "DELETE FROM catalogo_rubricas WHERE codigo = ?", [codigo]
    )
    db.commit()
    return cur.rowcount > 0

