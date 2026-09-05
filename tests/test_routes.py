"""
test_routes.py
--------------
Testes dos endpoints principais e dos controles de autenticação/isolamento:

  * Rotas de página e API exigem sessão autenticada.
  * Fluxo de registro + login + acesso a dados do próprio usuário.
  * Endpoint /api/export (csv e xlsx) gera arquivo com MIME correto.
"""
import pytest

from database.connection import get_db


# ---------------------------------------------------------------------
# Controles de autenticação
# ---------------------------------------------------------------------
def test_index_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302  # redireciona para o login


def test_api_requires_login(client):
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/holerites").status_code == 401
    assert client.get("/api/export").status_code == 401


def test_login_page_available(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_register_creates_account_and_login(client, register_user, login):
    register_user(email="carol@example.com")
    resp = login(email="carol@example.com")
    assert resp.status_code == 200
    # Logado (usuário comum), acessa o dashboard em /dashboard.
    home = client.get("/dashboard")
    assert home.status_code == 200
    # A raiz redireciona o usuário comum para /dashboard (separação RBAC).
    root = client.get("/")
    assert root.status_code == 302
    assert "/dashboard" in root.headers["Location"]


def test_admin_root_redirects_to_admin(client, register_user, login, user_id_by_email, app):
    # Registra, loga como usuário comum e promove a conta a admin.
    register_user(email="admin@example.com")
    login(email="admin@example.com")
    with app.app_context():
        from models.user import set_user_role
        set_user_role(get_db(), user_id_by_email("admin@example.com"), "admin")
    client.get("/logout")
    login(email="admin@example.com")
    # Admin NÃO deve cair no dashboard pessoal: a raiz leva a /admin.
    root = client.get("/")
    assert root.status_code == 302
    assert "/admin" in root.headers["Location"]


def test_dashboard_page_requires_login(client):
    assert client.get("/dashboard").status_code == 302  # redireciona p/ login


# ---------------------------------------------------------------------
# Endpoints de dados (escopados por usuário)
# ---------------------------------------------------------------------
def test_summary_after_login(client, register_user, login):
    register_user(email="dani@example.com")
    login(email="dani@example.com")
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 0
    assert "total_earnings" in data and "net_value" in data


def test_holerites_list_after_login(client, register_user, login):
    register_user(email="edu@example.com")
    login(email="edu@example.com")
    resp = client.get("/api/holerites")
    assert resp.status_code == 200
    assert resp.get_json() == []


# ---------------------------------------------------------------------
# Exportação de dados
# ---------------------------------------------------------------------
def _setup_user_with_data(client, register_user, login, seed_paystub,
                          email="fabi@example.com"):
    register_user(email=email)
    login(email=email)
    seed_paystub(
        email, "2024-01", net=1200.0, base=1000.0, earnings=1300.0,
        deductions=100.0, codigo="1000", descricao="Salário Base",
        tipo="provento", valor=1000.0,
    )


def test_export_csv(client, register_user, login, seed_paystub):
    _setup_user_with_data(client, register_user, login, seed_paystub)
    resp = client.get("/api/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    body = resp.data.decode("utf-8-sig")
    assert "holerite_id" in body
    assert "2024-01" in body
    assert "Salário Base" in body


def test_export_xlsx(client, register_user, login, seed_paystub):
    _setup_user_with_data(client, register_user, login, seed_paystub)
    resp = client.get("/api/export?format=xlsx")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # Arquivo XLSX é um zip: inicia com a assinatura 'PK'.
    assert resp.data.startswith(b"PK")


def test_export_defaults_to_xlsx(client, register_user, login, seed_paystub):
    _setup_user_with_data(client, register_user, login, seed_paystub)
    resp = client.get("/api/export")
    assert resp.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_export_invalid_format(client, register_user, login):
    register_user(email="gabi@example.com")
    login(email="gabi@example.com")
    resp = client.get("/api/export?format=pdf")
    assert resp.status_code == 400


def test_export_empty_returns_headers(client, register_user, login):
    register_user(email="helo@example.com")
    login(email="helo@example.com")
    resp = client.get("/api/export?format=csv")
    assert resp.status_code == 200
    assert "holerite_id" in resp.data.decode("utf-8-sig")


def test_export_is_scoped_to_user(client, register_user, login, seed_paystub):
    # Usuário A tem dados; usuário B não deve vê-los no export.
    register_user(email="a@example.com")
    login(email="a@example.com")
    seed_paystub("a@example.com", "2024-01", net=1200.0)

    # Encerra a sessão de A antes de criar/logar como B.
    client.get("/logout")
    register_user(email="b@example.com")
    login(email="b@example.com")
    resp = client.get("/api/export?format=csv")
    body = resp.data.decode("utf-8-sig")
    assert "2024-01" not in body
# ---------------------------------------------------------------------
# Explicação de cards com IA (✨) — cota Freemium/Pro
# ---------------------------------------------------------------------
def _ai_used(client):
    return client.get("/api/plan").get_json()["ai"]["used"]


def _fill_usage(db, user_id, count):
    for _ in range(int(count)):
        db.execute("INSERT INTO ai_usage_logs (user_id) VALUES (?)", [user_id])
    db.commit()


def test_explain_card_success_decrements_quota(client, register_user, login):
    register_user(email="card_ok@example.com")
    login(email="card_ok@example.com")

    before = _ai_used(client)
    resp = client.post(
        "/api/analytics/explain-card",
        json={"card_id": "recurrent_net", "month": "", "company": ""},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["card_id"] == "recurrent_net"
    # 3 bullets ("O que representa", "Insight do período", "Como é calculado").
    md = data["markdown"]
    assert md.count("- **O que representa:**") == 1
    assert md.count("- **Insight do período:**") == 1
    assert md.count("- **Como é calculado:**") == 1
    # Consumiu 1 crédito da cota.
    assert _ai_used(client) == before + 1


def test_explain_card_free_reaches_rate_limit_429(client, register_user, login, db, user_id_by_email):
    email = "card_free@example.com"
    register_user(email=email)
    login(email=email)
    uid = user_id_by_email(email)
    _fill_usage(db, uid, 5)  # limite Free: 5 consultas / 24h

    resp = client.post(
        "/api/analytics/explain-card",
        json={"card_id": "projecao_anual", "month": "2026-04", "company": ""},
    )
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["status"] == "rate_limit_exceeded"
    assert "atingiu o limite" in body["message"]
    assert "retry_after_seconds" in body


def test_explain_card_pro_reaches_rate_limit_429(client, register_user, login, db, user_id_by_email, app):
    email = "card_pro@example.com"
    register_user(email=email)
    login(email=email)
    uid = user_id_by_email(email)
    with app.app_context():
        from models.user import set_user_plan
        set_user_plan(get_db(), uid, "pro")
    client.get("/logout")
    login(email=email)  # atualiza a sessão com o plano Pro

    _fill_usage(db, uid, 15)  # limite Pro: 15 consultas / 1h
    resp = client.post(
        "/api/analytics/explain-card",
        json={"card_id": "salario_hora", "month": "2026-05", "company": ""},
    )
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["status"] == "rate_limit_exceeded"
    assert "retry_after_seconds" in body


def test_explain_card_unknown_card_400(client, register_user, login):
    register_user(email="card_bad@example.com")
    login(email="card_bad@example.com")
    resp = client.post(
        "/api/analytics/explain-card",
        json={"card_id": "inexistente", "month": "", "company": ""},
    )
    assert resp.status_code == 400

