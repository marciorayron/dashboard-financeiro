"""
services/auth_service.py
------------------------
Helpers de autorização do modelo Freemium (Free vs Pro).

Centraliza as políticas de limite que dependem do plano do usuário:

  * ``PaystubLimitError``            -> HTTP 402 (limite de holerites).
  * ``enforce_paystub_upload_limit`` -> trava uploads acima do teto do plano
    Gratuito (máx. 3 holerites).
  * ``get_user_plan`` / ``current_user_plan`` -> leitura do plano.

O rate limiting de IA (ask-ai / explain-anomaly) vive em
``services/ai_service.py``; este módulo foca no limite de armazenamento de
holerites e na leitura do plano da sessão.
"""
from flask import session

from models.user import PLAN_FREE, PLAN_PRO
from services.ai_service import get_user_plan


class PaystubLimitError(Exception):
    """Usuário atingiu o limite de holerites do plano."""

    status_code = 402

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(
            "Você atingiu o limite de holerites do plano Gratuito."
        )

    def payload(self) -> dict:
        return {
            "error": "LIMIT_EXCEEDED",
            "message": (
                f"Você atingiu o limite de {self.limit} holerites do plano "
                "Gratuito. Faça o upgrade para o Plano Pró."
            ),
        }


def get_paystub_limit(db, plan: str):
    """
    Retorna o teto de holerites do plano (None = ilimitado).

    O limite do plano Gratuito é dinâmico (editável no painel admin via
    `app_settings`), com fallback para o padrão de 3 holerites.
    """
    if plan == PLAN_PRO:
        return None
    from services.settings_service import get_freemium_limits

    return int(get_freemium_limits(db)["free_paystub_limit"])


def count_user_paystubs(db, user_id: int) -> int:
    """Total de holerites armazenados para o usuário."""
    row = db.execute(
        "SELECT COUNT(*) AS c FROM holerites WHERE user_id = ?", [user_id]
    ).fetchone()
    return int(row["c"]) if row else 0


def enforce_paystub_upload_limit(db, user_id: int) -> dict:
    """
    Aplica o limite de upload de holerites conforme o plano (limite dinâmico).

    Returns:
        dict: {"plan", "limit", "used", "remaining"}

    Raises:
        PaystubLimitError: quando um usuário Free já atingiu o teto de
        holerites (HTTP 402).
    """
    plan = get_user_plan(db, user_id)
    limit = get_paystub_limit(db, plan)
    used = count_user_paystubs(db, user_id)
    if limit is not None and used >= limit:
        raise PaystubLimitError(limit)
    return {
        "plan": plan,
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(limit - used, 0),
    }


def current_user_plan() -> str:
    """Plano do usuário autenticado na sessão (default 'free')."""
    plan = session.get("user_plan") or PLAN_FREE
    return plan if plan in (PLAN_FREE, PLAN_PRO) else PLAN_FREE
