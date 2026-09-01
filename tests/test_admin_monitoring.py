"""
tests/test_admin_monitoring.py
-------------------------------
Testes da expansão do painel administrativo como hub de gestão do sistema:

  1. RBAC: apenas admins acessam `/admin/api/metrics` e `/admin/api/errors`.
  2. `/admin/api/metrics` expõe saúde do banco, usuários ativos, holerites,
     PDFs e uso de armazenamento.
  3. Fluxo de auditoria de erros de parsing (registrar, listar, resolver).
"""
import pytest

from database.connection import get_db
from models.user import set_user_role
from services.monitoring_service import record_parse_error

ADMIN_EMAIL = "admin_ops@example.com"
USER_EMAIL = "user_ops@example.com"
PASSWORD = "senha123"


@pytest.fixture()
def admin_client(client, register_user, login, user_id_by_email, app):
    """Client logado como admin (conta promovida)."""
    register_user(name="Admin Ops", email=ADMIN_EMAIL, password=PASSWORD)
    login(email=ADMIN_EMAIL)
    with app.app_context():
        set_user_role(get_db(), user_id_by_email(ADMIN_EMAIL), "admin")
    return client


def _login_as_user(client, login, register_user):
    register_user(name="Usuário Ops", email=USER_EMAIL, password=PASSWORD)
    login(email=USER_EMAIL)


# ---------------------------------------------------------------------
# 1) RBAC nos endpoints de monitoramento
# ---------------------------------------------------------------------
def test_metrics_require_admin(client, register_user, login):
    _login_as_user(client, login, register_user)
    assert client.get("/admin/api/metrics").status_code == 403


def test_errors_require_admin(client, register_user, login):
    _login_as_user(client, login, register_user)
    assert client.get("/admin/api/errors").status_code == 403


# ---------------------------------------------------------------------
# 2) Saúde do sistema (Database Status / Storage)
# ---------------------------------------------------------------------
def test_metrics_returns_system_health(admin_client, db, user_id_by_email):
    # Cria um holerite para o admin para validar contagens/armazenamento.
    with admin_client.application.app_context():
        from database.connection import get_db as gd
        cur = gd().execute(
            "INSERT INTO holerites (user_id, company_name, mes_referencia, "
            "tipo_documento, totals, file_path) "
            "VALUES (?, 'Empresa', '2024-01', 'FOLHA_MENSAL', '{}', '/tmp/x.pdf')",
            [user_id_by_email(ADMIN_EMAIL)],
        )
        gd().commit()
        assert cur.lastrowid

    resp = admin_client.get("/admin/api/metrics")
    assert resp.status_code == 200
    m = resp.get_json()
    assert m["db"]["status"] == "ok"
    assert m["total_users"] >= 1
    assert m["active_users"] >= 1
    assert m["total_paystubs"] >= 1
    assert m["pdf_uploads"] >= 1
    assert m["storage_bytes"] >= 0
    assert "unresolved_parsing_errors" in m


# ---------------------------------------------------------------------
# 3) Auditoria de erros de parsing
# ---------------------------------------------------------------------
def test_parse_error_log_flow(admin_client, db, user_id_by_email):
    uid = user_id_by_email(USER_EMAIL) or user_id_by_email(ADMIN_EMAIL)

    # Registra um erro de parsing para um usuário.
    error_id = record_parse_error(
        db, user_id=uid, filename="holerite.pdf",
        error_type="ValueError", error_message="coluna ausente",
        traceback="Traceback (most recent call last): ...",
    )

    # Admin lista o erro (com contexto do usuário).
    resp = admin_client.get("/admin/api/errors?resolved=0")
    assert resp.status_code == 200
    items = resp.get_json()
    assert any(i["id"] == error_id for i in items)
    erro = next(i for i in items if i["id"] == error_id)
    assert erro["filename"] == "holerite.pdf"
    assert erro["error_type"] == "ValueError"
    assert erro["user_id"] == uid

    # Marca como resolvido.
    resolved = admin_client.post(f"/admin/api/errors/{error_id}/resolve")
    assert resolved.status_code == 200

    # Não deve mais aparecer na listagem de pendentes.
    resp = admin_client.get("/admin/api/errors?resolved=0")
    assert all(i["id"] != error_id for i in resp.get_json())


def test_resolve_missing_error_404(admin_client):
    resp = admin_client.post("/admin/api/errors/999999/resolve")
    assert resp.status_code == 404
