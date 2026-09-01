"""
main.py
-------
Blueprint responsável pelas rotas de páginas (HTML).

Renderiza o dashboard e a página inicial usando o template `index.html`.
Todas as rotas de página exigem sessão autenticada (`login_required`),
exceto o healthcheck (público).
"""
from flask import Blueprint, current_app, redirect, render_template, session, url_for

from database.connection import get_db
from routes.auth import login_required
from models.user import is_admin

main_bp = Blueprint("main", __name__)


def _home_target(db, user_id) -> str:
    """Endpoint de destino conforme o papel: admin -> /admin, usuário -> /dashboard."""
    return "admin.index" if is_admin(db, user_id) else "main.dashboard"


@main_bp.route("/")
@login_required
def index():
    """
    Dispatcher por papel.

    * Admin   -> redireciona para `/admin` (sistema de gestão).
    * Usuário -> redireciona para `/dashboard` (dashboard financeiro).

    Garante que o admin NÃO caia por padrão no dashboard pessoal e que o
    usuário comum nunca seja enviado ao painel administrativo pela raiz.
    """
    db = get_db()
    return redirect(url_for(_home_target(db, session.get("user_id"))))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    """Página do dashboard financeiro pessoal (padrão do usuário comum)."""
    db = get_db()
    user_is_admin = is_admin(db, session.get("user_id"))
    return render_template(
        "index.html",
        title="Dashboard Financeiro Pessoal",
        user_name=session.get("user_name", "Usuário"),
        is_admin=user_is_admin,
        deepseek_enabled=bool(current_app.config.get("DEEPSEEK_API_KEY")),
    )


@main_bp.route("/health")
def health():
    """Endpoint público de healthcheck para orquestração/Docker."""
    return {"status": "ok", "app": "dashboard-financeiro"}

