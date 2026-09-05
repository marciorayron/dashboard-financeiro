"""
tests/test_admin.py
--------------------
Testes do backoffice SaaS (painel administrativo):

  1. RBAC: apenas admins acessam os novos endpoints de gestão.
  2. Troca de plano de assinatura via API admin (free <-> pro).
  3. Reset de consumo de IA (ai_usage_logs / ai_blocked_logs).
  4. Limites Freemium dinâmicos (editáveis no painel).
"""
import pytest

from database.connection import get_db
from models.user import get_user_by_email, set_user_role

ADMIN_EMAIL = "admin_saas@example.com"
USER_EMAIL = "user_saas@example.com"
PASSWORD = "senha123"


@pytest.fixture()
def admin_client(client, register_user, login, user_id_by_email, app):
    """Client logado como admin. Também registra um usuário comum alvo."""
    register_user(email=USER_EMAIL)
    register_user(email=ADMIN_EMAIL)
    login(email=ADMIN_EMAIL)
    with app.app_context():
        set_user_role(get_db(), user_id_by_email(ADMIN_EMAIL), "admin")
    return client


@pytest.fixture()
def user_client(client, register_user, login, user_id_by_email, app):
    """Registra um usuário comum e retorna o client logado como ele."""
    register_user(email=USER_EMAIL)
    login(email=USER_EMAIL)
    return client


def _uid(app, email):
    with app.app_context():
        return get_user_by_email(get_db(), email).id


# ---------------------------------------------------------------------
# 1) Autorização: não-admins recebem 403 nos novos endpoints
# ---------------------------------------------------------------------
def test_admin_new_endpoints_require_admin(user_client):
    assert user_client.get("/admin/api/stats").status_code == 403
    assert user_client.get("/admin/api/ai-usage").status_code == 403
    assert user_client.get("/admin/api/freemium-limits").status_code == 403
    assert user_client.get("/admin/api/storage").status_code == 403


def test_admin_user_plan_requires_admin(user_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = user_client.post(f"/admin/api/users/{uid}/plan", json={"plan": "pro"})
    assert resp.status_code == 403


def test_admin_reset_usage_requires_admin(user_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = user_client.post(f"/admin/api/users/{uid}/reset-usage")
    assert resp.status_code == 403


def test_admin_freemium_put_requires_admin(user_client):
    resp = user_client.put("/admin/api/freemium-limits", json={"free_paystub_limit": 4})
    assert resp.status_code == 403


def test_new_operational_endpoints_require_admin(user_client):
    assert user_client.post("/admin/api/users/create",
                            json={"name": "X", "email": "x@example.com",
                                  "password": "senha123"}).status_code == 403
    assert user_client.get("/admin/api/ai-config").status_code == 403
    assert user_client.put("/admin/api/ai-config", json={"model": "gpt-4o-mini"}).status_code == 403
    assert user_client.post("/admin/api/users/1/toggle-status").status_code == 403
    assert user_client.post("/admin/api/users/1/delete").status_code == 403
    assert user_client.post("/admin/api/users/1/reset-holerites").status_code == 403


# ---------------------------------------------------------------------
# 2) Troca de plano de assinatura via API admin
# ---------------------------------------------------------------------
def test_admin_change_plan_roundtrip(admin_client, app):
    uid = _uid(app, USER_EMAIL)

    # free -> pro
    resp = admin_client.post(f"/admin/api/users/{uid}/plan", json={"plan": "pro"})
    assert resp.status_code == 200
    assert resp.get_json()["plan"] == "pro"

    # pro -> free
    resp = admin_client.post(f"/admin/api/users/{uid}/plan", json={"plan": "free"})
    assert resp.status_code == 200
    assert resp.get_json()["plan"] == "free"


def test_admin_change_plan_invalid_plan(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = admin_client.post(f"/admin/api/users/{uid}/plan", json={"plan": "enterprise"})
    assert resp.status_code == 400


def test_admin_change_plan_missing_user_404(admin_client):
    resp = admin_client.post("/admin/api/users/999999/plan", json={"plan": "pro"})
    assert resp.status_code == 404


def test_admin_create_user(admin_client):
    resp = admin_client.post(
        "/admin/api/users/create",
        json={"name": "Novo Usuário", "email": "novo@example.com",
              "password": "novaSenha123", "role": "user", "plan": "pro"},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["plan"] == "pro"
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert "password_hash" not in body

    # Persistido com hash (login funciona).
    login = admin_client.post("/login", data={"email": "novo@example.com",
                                              "password": "novaSenha123"})
    assert login.status_code in (200, 302)


def test_admin_create_user_duplicate_email(admin_client):
    resp = admin_client.post(
        "/admin/api/users/create",
        json={"name": "Duplicado", "email": USER_EMAIL, "password": "senha123"},
    )
    assert resp.status_code == 400


def test_admin_create_user_short_password(admin_client):
    resp = admin_client.post(
        "/admin/api/users/create",
        json={"name": "Curto", "email": "curto@example.com", "password": "123"},
    )
    assert resp.status_code == 400


def test_admin_delete_user_removes_account(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = admin_client.post(f"/admin/api/users/{uid}/delete")
    assert resp.status_code == 200
    with app.app_context():
        assert get_user_by_email(get_db(), USER_EMAIL) is None


def test_admin_toggle_status_suspend_and_reinstate(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    suspended = admin_client.post(f"/admin/api/users/{uid}/toggle-status",
                                  json={"active": False})
    assert suspended.status_code == 200
    assert suspended.get_json()["is_active"] is False

    reinstated = admin_client.post(f"/admin/api/users/{uid}/toggle-status",
                                   json={"active": True})
    assert reinstated.status_code == 200
    assert reinstated.get_json()["is_active"] is True


def test_admin_reset_holerites(admin_client, db, app):
    uid = _uid(app, USER_EMAIL)
    for mes in ("2026-01", "2026-02"):
        db.execute(
            "INSERT INTO holerites (user_id, company_name, mes_referencia, "
            "tipo_documento, totals) VALUES (?, 'E', ?, 'FOLHA_MENSAL', '{}')",
            [uid, mes],
        )
    db.commit()
    resp = admin_client.post(f"/admin/api/users/{uid}/reset-holerites")
    assert resp.status_code == 200
    assert resp.get_json()["holerites_cleared"] == 2
    with app.app_context():
        left = db.execute(
            "SELECT COUNT(*) AS c FROM holerites WHERE user_id = ?", [uid]
        ).fetchone()["c"]
    assert left == 0


# ---------------------------------------------------------------------
# Reset de senha (temporária e explícita)
# ---------------------------------------------------------------------
def test_admin_reset_password_temporary(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = admin_client.post(f"/admin/api/users/{uid}/reset-password")
    assert resp.status_code == 200
    assert "temporary_password" in resp.get_json()


def test_admin_reset_password_explicit(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = admin_client.post(
        f"/admin/api/users/{uid}/reset-password", json={"password": "novaSenha789"}
    )
    assert resp.status_code == 200

    # A nova senha permite autenticar como o usuário alvo.
    admin_client.get("/logout")
    ok = admin_client.post("/login", data={"email": USER_EMAIL,
                                           "password": "novaSenha789"})
    assert ok.status_code in (200, 302)


def test_admin_reset_password_short(admin_client, app):
    uid = _uid(app, USER_EMAIL)
    resp = admin_client.post(
        f"/admin/api/users/{uid}/reset-password", json={"password": "123"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# 3) Reset de consumo de IA (suporte)
# ---------------------------------------------------------------------
def test_admin_reset_usage_clears_logs(admin_client, db, app):
    uid = _uid(app, USER_EMAIL)

    from services.ai_service import ensure_ai_blocked_logs_table, log_ai_usage

    log_ai_usage(db, uid)
    log_ai_usage(db, uid)
    ensure_ai_blocked_logs_table(db)
    db.execute(
        "INSERT INTO ai_blocked_logs (user_id, reason, detail) VALUES (?, ?, ?)",
        [uid, "prompt_injection", "tentativa"],
    )
    db.commit()

    with app.app_context():
        before = db.execute(
            "SELECT COUNT(*) AS c FROM ai_usage_logs WHERE user_id = ?", [uid]
        ).fetchone()["c"]
    assert before == 2

    resp = admin_client.post(f"/admin/api/users/{uid}/reset-usage")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["queries_cleared"] == 2
    assert body["blocked_cleared"] == 1

    with app.app_context():
        after = db.execute(
            "SELECT COUNT(*) AS c FROM ai_usage_logs WHERE user_id = ?", [uid]
        ).fetchone()["c"]
    assert after == 0


def test_admin_reset_usage_missing_user_404(admin_client):
    resp = admin_client.post("/admin/api/users/999999/reset-usage")
    assert resp.status_code == 404


# ---------------------------------------------------------------------
# 4) Limites Freemium dinâmicos
# ---------------------------------------------------------------------
def test_freemium_limits_get_and_put(admin_client):
    resp = admin_client.get("/admin/api/freemium-limits")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["free_paystub_limit"] == 3
    assert data["free_ai_queries_per_day"] == 5

    resp = admin_client.put("/admin/api/freemium-limits", json={"free_paystub_limit": 6})
    assert resp.status_code == 200
    assert resp.get_json()["free_paystub_limit"] == 6


def test_dynamic_paystub_limit_allows_fourth(admin_client, db, app):
    """Ao elevar o limite Freemium, um usuário Free com 3 holerites deixa de
    ser bloqueado no 4º upload."""
    from services.auth_service import PaystubLimitError, enforce_paystub_upload_limit

    uid = _uid(app, USER_EMAIL)
    for mes in ("2026-01", "2026-02", "2026-03"):
        db.execute(
            "INSERT INTO holerites (user_id, company_name, mes_referencia, "
            "tipo_documento, totals) VALUES (?, 'E', ?, 'FOLHA_MENSAL', '{}')",
            [uid, mes],
        )
    db.commit()

    # Default (3): bloqueia o 4º.
    with pytest.raises(PaystubLimitError):
        enforce_paystub_upload_limit(db, uid)

    # Eleva o limite para 5 -> passa a permitir.
    admin_client.put("/admin/api/freemium-limits", json={"free_paystub_limit": 5})
    result = enforce_paystub_upload_limit(db, uid)
    assert result["limit"] == 5


# ---------------------------------------------------------------------
# 5) Precificação dinâmica de tokens (IA / FinOps)
# ---------------------------------------------------------------------
def test_dynamic_token_pricing_calculation():
    """Custo = (input_tokens/1M * input_rate) + (output_tokens/1M * output_rate)."""
    from services.admin_service import estimate_ai_cost

    res = estimate_ai_cost(
        1000, 2000,
        {"model": "gpt-4o-mini", "input_rate_per_million": 1.0,
         "output_rate_per_million": 2.0},
    )
    assert res["model"] == "gpt-4o-mini"
    assert res["input_tokens"] == 1000
    assert res["output_tokens"] == 2000
    assert res["estimated_tokens"] == 3000
    # (1000/1e6 * 1.0) + (2000/1e6 * 2.0) = 0.001 + 0.004 = 0.005
    assert res["estimated_cost"] == pytest.approx(0.005)


def test_ai_config_persist_and_split_token_stats(admin_client, db, app):
    # GET default.
    default = admin_client.get("/admin/api/ai-config")
    assert default.status_code == 200
    assert default.get_json()["model"] == "deepseek-chat"

    # Persiste modelo e tarifas.
    saved = admin_client.put(
        "/admin/api/ai-config",
        json={"model": "gpt-4o-mini", "input_rate_per_million": 0.15,
              "output_rate_per_million": 0.60},
    )
    assert saved.status_code == 200
    cfg = saved.get_json()
    assert cfg["model"] == "gpt-4o-mini"
    assert cfg["input_rate_per_million"] == pytest.approx(0.15)
    assert cfg["output_rate_per_million"] == pytest.approx(0.60)

    # Insere 3 consultas de IA para o usuário-alvo (estimativa por consulta).
    from services.ai_service import log_ai_usage

    uid = _uid(app, USER_EMAIL)
    for _ in range(3):
        log_ai_usage(db, uid)

    s = admin_client.get("/admin/api/stats").get_json()["ai"]
    assert s["model"] == "gpt-4o-mini"
    assert s["input_rate_per_million"] == pytest.approx(0.15)
    assert s["output_rate_per_million"] == pytest.approx(0.60)
    # Split de contadores: 300 input + 100 output por consulta.
    assert s["input_tokens"] == s["total_queries"] * 300
    assert s["output_tokens"] == s["total_queries"] * 100
    assert s["estimated_tokens"] == s["input_tokens"] + s["output_tokens"]

    expected = round((s["input_tokens"] / 1e6) * 0.15 +
                     (s["output_tokens"] / 1e6) * 0.60, 4)
    assert s["estimated_cost"] == pytest.approx(expected)


