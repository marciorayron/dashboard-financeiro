"""
routes
======
Pacote com os blueprints (controladores) da aplicação.

Separação de responsabilidades:
  * `auth.py`   -> autenticação por sessão (login/register/logout).
  * `main.py`   -> rotas de páginas (renderização de templates HTML).
  * `api.py`    -> rotas de API (upload, consultas, exclusão) em JSON.
  * `admin.py`  -> painel administrativo (/admin) com RBAC.
"""
from .admin import admin_bp
from .api import api_bp
from .auth import auth_bp
from .main import main_bp
from .profile import profile_bp
from .reports import reports_bp


def register_blueprints(app):
    """Registra todos os blueprints da aplicação."""
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(profile_bp, url_prefix="/api")
    app.register_blueprint(reports_bp, url_prefix="/api")
    app.register_blueprint(admin_bp)


__all__ = [
    "register_blueprints",
    "admin_bp",
    "main_bp",
    "auth_bp",
    "api_bp",
    "profile_bp",
    "reports_bp",
]

