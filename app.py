"""
app.py
------
Ponto de entrada da aplicação Flask "Dashboard Financeiro Pessoal".

Responsabilidades:
  1. Criar e configurar a aplicação (`create_app` - app factory).
  2. Carregar a configuração adequada por ambiente.
  3. Registrar o teardown da conexão SQLite.
  4. Inicializar o schema do banco.
  5. Registrar os blueprints (páginas + API).
  6. Expor a instância `app` para o servidor (gunicorn/flask run).

Execução local (ambiente virtual — onde estão as dependências):
    .venv\\Scripts\\python.exe app.py
    # ou, com o venv ativado:  python app.py
"""
import logging
import warnings

# Suprime aviso cosmético de depreciação do pypdf/cryptography (não afeta o
# funcionamento) — é apenas um aviso de biblioteca em Python 3.13+.
warnings.filterwarnings("ignore", message="ARC4 has been moved")

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    request,
    session,
)

from config import get_config
from database.connection import close_db, init_db
from routes import register_blueprints


# ---------------------------------------------------------------------
# Onboarding obrigatório (guard global)
# ---------------------------------------------------------------------
# Rotas liberadas mesmo com o perfil incompleto (login/logout/onboarding,
# o endpoint de perfil usado pelo funil e assets estáticos).
_ONBOARDING_EXEMPT_PATHS = frozenset(
    {"/onboarding", "/api/profile", "/register", "/login", "/logout"}
)
_ONBOARDING_EXEMPT_PREFIXES = ("/static/", "/auth/",)


def _onboarding_allowed(path: str) -> bool:
    """True quando a rota pode ser acessada com o perfil ainda incompleto."""
    if path in _ONBOARDING_EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in _ONBOARDING_EXEMPT_PREFIXES)


def _onboarding_wants_json() -> bool:
    """True quando o cliente prefere resposta JSON (APIs)."""
    best = request.accept_mimetypes.best or ""
    return request.is_json or ("json" in best)


def _enforce_onboarding():
    """Force redirect/block enquanto `users.profile_completed` for falso.

    Um usuário autenticado com o perfil incompleto só pode acessar as rotas
    da whitelist (`/onboarding`, `/api/profile`, `/register`, `/login`,
    `/logout`, `/static/*` e `/auth/*`). Páginas são redirecionadas para
    `/onboarding`; chamadas de API recebem HTTP 403 `ONBOARDING_REQUIRED`.
    """
    user_id = session.get("user_id")
    if user_id is None:
        return None
    if _onboarding_allowed(request.path):
        return None

    from models.user import get_user  # import local evita ciclo

    try:
        user = get_user(get_db_conn(), user_id)
    except Exception:  # noqa: BLE001 - nunca deixar o guard quebrar o app
        user = None
    if user is not None and not getattr(user, "profile_completed", True):
        api_like = request.path.startswith("/api/") or request.path.startswith("/admin/api/")
        if api_like or _onboarding_wants_json():
            return (
                jsonify(
                    {
                        "error": "Perfil não configurado. Complete o onboarding.",
                        "code": "ONBOARDING_REQUIRED",
                    }
                ),
                403,
            )
        flash("Complete seu perfil para acessar o dashboard.", "warning")
        return redirect("/onboarding")
    return None


def get_db_conn():
    """Conexão SQLite do contexto atual (compat com o guard)."""
    from database.connection import get_db

    return get_db()


def create_app(env: str = None) -> Flask:
    """Factory que cria e configura uma instância da aplicação."""
    app = Flask(__name__)

    # 1) Carrega a configuração (por ambiente, default development).
    app.config.from_object(get_config(env))

    # 1.1) Serialização JSON robusta: garante que nenhuma resposta contenha
    # NaN / Infinity / -Infinity (literais inválidos para o parser do navegador).
    from services.safe_json import SafeJSONProvider

    app.json = SafeJSONProvider(app)

    # 1.2) Overrides administrativos persistidos (IA provider, paths). Aplicados
    # antes da inicialização do banco; ignorados no ambiente de teste.
    from services.system_config import apply_to_config

    apply_to_config(app, env)

    # 2) Configuração de logging.
    logging.basicConfig(
        level=logging.DEBUG if app.config.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # 3) Registra o teardown para fechar a conexão do request.
    app.teardown_appcontext(close_db)

    # 4) Garante que os diretórios de dados/upload existam.
    from pathlib import Path

    # Diretório do banco (o pai do arquivo .db).
    db_dir = Path(app.config["DATABASE_PATH"]).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    # Diretório de uploads.
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # 5) Cria as tabelas se ainda não existirem.
    with app.app_context():
        init_db()

        # 5.0) Migração de propriedade: holerites órfãos passam para o admin.
        from database.connection import get_db as _get_db
        from database.queries import claim_holerites_for_admin

        claim_holerites_for_admin(_get_db())

        # 5.1) Catálogo oficial de rubricas + migrações de classificação.
        from database.connection import get_db
        from services.db_service import (
            fix_classification_from_catalog,
            fix_provento_classification,
            init_rubrica_catalog,
        )

        try:
            init_rubrica_catalog(get_db())
            # Heurística por palavra/prefixo (pega itens fora do catálogo) ANTES
            # do catálogo, que é autoritativo e sobrescreve por último (ex.:
            # '/B01' contém 'ADIANTAMENTO' mas é PROVENTO no catálogo).
            fix_provento_classification(get_db())
            result = fix_classification_from_catalog(get_db())
            if result["fixed"]:
                logger = logging.getLogger(__name__)
                logger.info(
                    "Migração de classificação no startup: %d rubricas corrigidas "
                    "pelo catálogo, %d holerites recalculados.",
                    result["fixed"],
                    result["recalculated"],
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Falha ao executar as migrações de classificação no startup."
            )

    # 6) Registra os blueprints.
    register_blueprints(app)

    # 6.1) Guard global de onboarding (redireciona/bloqueia perfis incompletos).
    app.before_request(_enforce_onboarding)

    # 7) Comando CLI: criar/recriar o administrador padrão (RBAC).
    _register_seed_admin_cli(app)

    return app


def _register_seed_admin_cli(app):
    """Registra `flask seed-admin` para provisionar/resetar o admin padrão."""
    import click

    @app.cli.command("seed-admin")
    @click.option(
        "--reset",
        is_flag=True,
        help="Força o re-provisionamento/reset do admin padrão mesmo que já exista.",
    )
    def seed_admin_command(reset):
        """Cria (ou recria, com --reset) a conta de administrador padrão."""
        from database.connection import get_db
        from database.seed import seed_default_admin

        with app.app_context():
            db = get_db()
            info = seed_default_admin(db, force=reset)
            if info["skipped"]:
                click.echo(
                    f"Admin já existente; nada a fazer. Use `flask seed-admin --reset` "
                    f"para recriar a conta {info['email']}."
                )
            elif info["created"]:
                click.echo(f"[SEED] Default admin user created: {info['email']}")
            else:
                click.echo(f"[SEED] Default admin user reset: {info['email']}")


# Instância padrão criada no import (para servidores WSGI e `flask run`).
app = create_app()


if __name__ == "__main__":
    # Execução direta: `python app.py`.
    # No Docker usamos gunicorn (ver Dockerfile).
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=app.config.get("DEBUG", False),
    )
