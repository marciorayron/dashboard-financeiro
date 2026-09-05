"""
conftest.py
-----------
Fixtures compartilhadas para a suíte de testes (pytest).

Cada teste recebe uma aplicação Flask com banco SQLite ISOLADO (criado em
arquivo temporário e descartado ao final), garantindo que nenhum teste toque
o banco de desenvolvimento ou compartilhe estado entre execuções.

Nota sobre "in-memory": com SQLite, `:memory:` cria um banco NOVO por
conexão. Como o Flask test client abre uma conexão por request, usar
`:memory:` literal faria cada request enxergar um banco vazio. O padrão
correto (e usado aqui) é um arquivo temporário descartável — mesmo efeito de
isolamento e velocidade, porém compartilhado entre requests da sessão.
"""
import json

import pytest

from app import create_app
from database.connection import get_db, init_db
from services.db_service import init_rubrica_catalog


@pytest.fixture()
def app(tmp_path):
    """Aplicação Flask com banco isolado (arquivo temporário)."""
    application = create_app("testing")
    application.config["TESTING"] = True
    application.config["DATABASE_PATH"] = str(tmp_path / "financeiro_test.db")
    # Mantém os testes de IA offline: sem chave real, o ai_service usa o
    # fallback local determinístico (nunca chama a API DeepSeek).
    application.config["DEEPSEEK_API_KEY"] = ""
    with application.app_context():
        init_db()
        init_rubrica_catalog(get_db())
    yield application


@pytest.fixture()
def client(app):
    """Test client do Flask para chamadas de rota."""
    return app.test_client()


@pytest.fixture()
def db(app):
    """Conexão com o banco da aplicação (contexto de app ativo no teste)."""
    with app.app_context():
        yield get_db()


@pytest.fixture()
def register_user(client):
    """Registra um usuário e devolve a resposta."""
    def _register(name="Teste", email="teste@example.com", password="senha123"):
        return client.post(
            "/register",
            data={
                "name": name,
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            follow_redirects=True,
        )

    return _register


@pytest.fixture()
def login(client):
    """Autentica um usuário e devolve a resposta."""
    def _login(email="teste@example.com", password="senha123"):
        return client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    return _login


@pytest.fixture()
def user_id_by_email(app):
    """Retorna o id de um usuário a partir do e-mail."""
    def _get(email):
        with app.app_context():
            row = get_db().execute(
                "SELECT id FROM users WHERE email = ?", [email]
            ).fetchone()
            return row["id"] if row else None

    return _get


@pytest.fixture()
def seed_paystub(db, user_id_by_email):
    """Insere um holerite (+ rubrica opcional) para um usuário pelo e-mail."""
    def _seed(
        email,
        mes,
        net,
        base=1000.0,
        earnings=1000.0,
        deductions=0.0,
        codigo=None,
        descricao=None,
        tipo="provento",
        valor=0.0,
    ):
        user_id = user_id_by_email(email)
        totals = json.dumps(
            {
                "base_salary": base,
                "total_earnings": earnings,
                "total_deductions": deductions,
                "net_value": net,
            }
        )
        cur = db.execute(
            "INSERT INTO holerites "
            "(user_id, company_name, mes_referencia, tipo_documento, totals) "
            "VALUES (?, ?, ?, 'FOLHA_MENSAL', ?)",
            [user_id, "Empresa Teste", mes, totals],
        )
        hid = cur.lastrowid
        if codigo:
            db.execute(
                "INSERT INTO rubricas_holerite "
                "(holerite_id, user_id, codigo, descricao, tipo, valor) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [hid, user_id, codigo, descricao or codigo, tipo, valor],
            )
        db.commit()
        return hid

    return _seed
