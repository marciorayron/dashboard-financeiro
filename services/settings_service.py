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
KEY_FREEMIUM = "freemium_limits"
KEY_AI_CONFIG = "ai_config"

# Limites padrão do modelo Freemium (usados quando não há override no banco).
DEFAULT_FREEMIUM_LIMITS = {
    "free_paystub_limit": 3,      # máx. de holerites no plano Gratuito
    "free_ai_queries_per_day": 5,  # consultas de IA por 24h (Free)
    "pro_ai_queries_per_hour": 15,  # consultas de IA por 1h (Pro)
}

# Configuração padrão do provedor/modelo de IA (modelo-agnóstico).
# Preços por 1M de tokens em US$ (input/output) usados no cálculo de FinOps.
DEFAULT_AI_CONFIG = {
    "model": "deepseek-chat",
    "input_rate_per_million": 0.27,
    "output_rate_per_million": 1.10,
}


def get_ai_config(db) -> dict:
    """Retorna a configuração ativa do modelo/tarifas de IA (com fallback)."""
    stored = get_settings(db, KEY_AI_CONFIG, None)
    if not isinstance(stored, dict):
        stored = {}
    config = dict(DEFAULT_AI_CONFIG)
    if stored.get("model"):
        config["model"] = str(stored["model"]).strip()
    for key in ("input_rate_per_million", "output_rate_per_million"):
        value = stored.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            config[key] = float(value)
    return config


def save_ai_config(db, data: dict) -> dict:
    """
    Persiste a configuração do provedor/modelo e tarifas de tokens da IA.

    Args:
        data (dict): {"model": str, "input_rate_per_million": float,
                      "output_rate_per_million": float}

    Returns:
        dict: a configuração salva (via `get_ai_config`).
    """
    data = data or {}
    current = get_ai_config(db)
    if data.get("model"):
        current["model"] = str(data["model"]).strip()
    for key in ("input_rate_per_million", "output_rate_per_million"):
        value = data.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            current[key] = round(parsed, 8)
    set_settings(db, KEY_AI_CONFIG, current)
    return get_ai_config(db)


def get_freemium_limits(db) -> dict:
    """Retorna os limites dinâmicos do modelo Freemium (com fallback padrão)."""
    stored = get_settings(db, KEY_FREEMIUM, None)
    if not isinstance(stored, dict):
        stored = {}
    limits = dict(DEFAULT_FREEMIUM_LIMITS)
    for key in DEFAULT_FREEMIUM_LIMITS:
        value = stored.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            limits[key] = int(value)
    return limits


def save_freemium_limits(db, data: dict) -> dict:
    """
    Persiste os limites do modelo Freemium editados no painel admin.

    Args:
        data (dict): {"free_paystub_limit": int, "free_ai_queries_per_day": int,
                      "pro_ai_queries_per_hour": int}

    Returns:
        dict: os limites salvos (via `get_freemium_limits`).
    """
    data = data or {}
    current = get_freemium_limits(db)
    for key in DEFAULT_FREEMIUM_LIMITS:
        value = data.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            current[key] = int(value)
    set_settings(db, KEY_FREEMIUM, current)
    return get_freemium_limits(db)


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

