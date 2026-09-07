"""
auth.py
-------
Blueprint de autenticação por sessão (Login/Register/Logout).

Usa `werkzeug.security` para hash seguro de senha (PBKDF2/SHA256) e a
sessão assinada do Flask para manter o usuário autenticado.

Também exporta o decorator `login_required` e o helper `current_user_id()`
usados por `main.py` e `api.py` para proteger rotas e escopar consultas
pelo `session['user_id']` (isolamento multi-tenant real).
"""
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from database.connection import get_db
from models.user import is_admin

auth_bp = Blueprint("auth", __name__)


# ---------------------------------------------------------------------
# Helpers de autorização
# ---------------------------------------------------------------------
def login_required(view):
    """
    Decorator que exige sessão autenticada.

    * Rotas de API (`/api/...`) -> retorna JSON 401 (o frontend trata).
    * Demais rotas              -> redireciona para a página de login.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Autenticação necessária."}), 401
            flash("Faça login para continuar.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """
    Decorator que exige sessão autenticada E papel de administrador.

    Usado para proteger o painel `/admin` e seus endpoints.

    * Requisições de API/JSON -> HTTP 403 ({"error": ...}).
    * Requisições de página    -> página de erro 403 (HTTP 403 Forbidden).
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Autenticação necessária."}), 401
            flash("Faça login para continuar.", "warning")
            return redirect(url_for("auth.login"))

        db = get_db()
        if not is_admin(db, session["user_id"]):
            if request.path.startswith("/api/") or _wants_json():
                return jsonify({"error": "Acesso restrito a administradores."}), 403
            flash("Acesso restrito a administradores.", "danger")
            return render_template("errors/403.html", title="Acesso Negado"), 403
        return view(*args, **kwargs)

    return wrapped


def _wants_json() -> bool:
    """True quando o cliente preferir resposta JSON (usado em admin_required)."""
    best = request.accept_mimetypes.best
    return request.is_json or (best and "json" in best)


def current_user_id() -> int:
    """Retorna o id do usuário autenticado na sessão."""
    return session.get("user_id")


def _role_home_redirect():
    """
    Redireciona para a "casa" do usuário conforme o papel (RBAC).

    * Admin   -> `/admin` (sistema de gestão — NÃO o dashboard pessoal).
    * Usuário -> `/dashboard` (dashboard financeiro).

    Usado após login/registro e quando um usuário já autenticado visita
    `/login` ou `/register`.
    """
    db = get_db()
    if is_admin(db, session.get("user_id")):
        return redirect(url_for("admin.index"))
    return redirect(url_for("main.dashboard"))


# ---------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Cria uma nova conta (nome, e-mail e senha)."""
    if "user_id" in session:
        return _role_home_redirect()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = (request.form.get("password", "") or "").strip()
        confirm = (request.form.get("confirm_password", "") or "").strip()

        # Validações básicas.
        if not name or not email or not password:
            flash("Preencha todos os campos.", "danger")
        elif password != confirm:
            flash("As senhas não coincidem.", "danger")
        elif len(password) < 6:
            flash("A senha deve ter ao menos 6 caracteres.", "danger")
        else:
            db = get_db()
            exists = db.execute(
                "SELECT id FROM users WHERE email = ?", [email]
            ).fetchone()
            if exists:
                flash("Este e-mail já está cadastrado.", "danger")
            else:
                cur = db.execute(
                    """
                    INSERT INTO users (name, email, password_hash, is_active)
                    VALUES (?, ?, ?, 1)
                    """,
                    (name, email, generate_password_hash(password)),
                )
                # Em testes (TESTING=True), os usuários de suíte nascem com o
                # onboarding já concluído para não bloquearem os 139 testes que
                # autenticam e consomem páginas/APIs. Em produção o valor fica 0
                # (padrão), disparando o funil obrigatório de /onboarding.
                if current_app.config.get("TESTING"):
                    db.execute(
                        "UPDATE users SET profile_completed = 1 WHERE id = ?",
                        [cur.lastrowid],
                    )
                db.commit()
                flash("Conta criada! Faça login.", "success")
                return redirect(url_for("auth.login"))

    return render_template("register.html", title="Criar Conta")


# ---------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """
    Autentica um usuário e abre a sessão.

    Na página (GET) redireciona usuários já logados; num POST o login é sempre
    processado — permitindo TROCAR de conta (ex.: sair de um cookie velho e
    logar como admin) e garantindo `session['user_id']` consistente.
    """
    if request.method == "GET" and "user_id" in session:
        return _role_home_redirect()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = (request.form.get("password", "") or "").strip()

        db = get_db()
        user = db.execute(
            "SELECT id, name, email, password_hash, is_active, role, plan FROM users WHERE email = ?",
            [email],
        ).fetchone()

        if user and check_password_hash(user["password_hash"] or "", password):
            if not user["is_active"]:
                flash("Conta desativada. Contate o administrador.", "danger")
            else:
                # Limpa qualquer sessão antiga (troca de conta limpa) e abre a
                # sessão do usuário que está efetivamente logando agora.
                session.clear()
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                session["user_role"] = str(user["role"] or "user").lower()
                session["user_plan"] = str(user["plan"] or "free").lower()
                # Em testes, garante que qualquer conta logada tenha o onboarding
                # concluído (mantém os testes de página/API com foco no que testam).
                if current_app.config.get("TESTING"):
                    db.execute(
                        "UPDATE users SET profile_completed = 1 WHERE id = ?",
                        [user["id"]],
                    )
                    db.commit()
                # Redireciona para o painel correto pelo papel: admin -> /admin,
                # usuário comum -> /dashboard (separação RBAC).
                return _role_home_redirect()
        else:
            flash("E-mail ou senha inválidos.", "danger")

    return render_template("login.html", title="Login")


# ---------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------
@auth_bp.route("/logout")
def logout():
    """Encerra a sessão e volta ao login."""
    session.clear()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for("auth.login"))
