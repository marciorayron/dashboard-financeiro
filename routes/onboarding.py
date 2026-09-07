"""routes/onboarding.py — Funil obrigatório de perfil (primeiro acesso).

Força o novo usuário a informar seus dados de contratação (data de admissão,
cargo, tipo de contrato, valor/hora ou salário base, jornada, dependentes IRRF)
antes de liberar o dashboard. Ao salvar, marca `users.profile_completed = 1`.
"""
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from database.connection import get_db
from models.profile import PROFILE_FIELDS, load_profile, save_profile, to_dict
from models.user import is_admin, set_user_profile_completed
from routes.auth import current_user_id, login_required

onboarding_bp = Blueprint("onboarding", __name__)


def _home_redirect():
    """Redireciona para a casa do usuário pelo papel (admin -> /admin)."""
    db = get_db()
    if is_admin(db, current_user_id()):
        return redirect(url_for("admin.index"))
    return redirect(url_for("main.dashboard"))


@onboarding_bp.route("/onboarding", methods=["GET"])
@login_required
def index():
    """Exibe o formulário de onboarding (só para quem ainda não concluiu)."""
    user_id = current_user_id()
    db = get_db()
    user = None
    try:
        from models.user import get_user

        user = get_user(db, user_id)
    except Exception:  # noqa: BLE001 - best-effort
        user = None
    # Usuários que já completaram o onboarding não precisam refazê-lo.
    if user is not None and getattr(user, "profile_completed", False):
        return _home_redirect()

    profile = load_profile(db, user_id)
    return render_template(
        "onboarding.html",
        title="Complete seu Perfil",
        profile=profile,
        profile_fields=PROFILE_FIELDS,
        user_name=session.get("user_name", ""),
    )


@onboarding_bp.route("/onboarding", methods=["POST"])
@login_required
def save():
    """Processa o formulário, salva o perfil e marca o onboarding como concluído."""
    user_id = current_user_id()
    db = get_db()

    # Campos numéricos podem vir vazios do formulário.
    numeric = ("base_rate", "monthly_hours", "irrf_dependents",
               "fixed_benefits_deduction", "overtime_tier1_rate",
               "overtime_tier1_limit", "overtime_tier2_rate")
    data = {}
    for field in PROFILE_FIELDS:
        value = (request.form.get(field) or "").strip()
        if field in numeric and value in ("", None):
            value = None
        data[field] = value

    try:
        profile = save_profile(db, user_id, data)
    except Exception:  # noqa: BLE001
        flash("Não foi possível salvar o perfil. Confira os dados.", "danger")
        return render_template(
            "onboarding.html",
            title="Complete seu Perfil",
            profile=load_profile(db, user_id),
            profile_fields=PROFILE_FIELDS,
            user_name=session.get("user_name", ""),
        ), 400

    set_user_profile_completed(db, user_id, True)
    flash("Perfil concluído. Bem-vindo(a)!", "success")
    return _home_redirect()
