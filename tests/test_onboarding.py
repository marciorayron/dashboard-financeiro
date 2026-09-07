"""test_onboarding.py — Funil obrigatório de perfil (guard global).

Valida que um usuário autenticado com `profile_completed = False`:
  * é redirecionado de páginas (`/dashboard`) para `/onboarding`;
  * recebe HTTP 403 `ONBOARDING_REQUIRED` em chamadas de `/api/*`;
  * só passa a acessar páginas/APIs após concluir o onboarding.
"""
from models.user import set_user_profile_completed


def _register_login(register_user, login, user_id_by_email, email):
    register_user(email=email)
    login(email=email)   # em TESTING o login marca profile_completed = 1
    return user_id_by_email(email)


def test_completed_user_not_blocked(client, register_user, login, db):
    """Linha de base: usuário com perfil concluído navega normalmente."""
    email = "onboard_ok@example.com"
    _register_login(register_user, login, lambda e: None, email)

    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/summary").status_code == 200


def test_incomplete_user_redirected_and_api_blocked(
    client, register_user, login, db, user_id_by_email
):
    """Usuário incompleto: página -> /onboarding; API -> 403 ONBOARDING_REQUIRED."""
    email = "onboard_blocked@example.com"
    uid = _register_login(register_user, login, user_id_by_email, email)

    # Simula um usuário recém-registrado que ainda não completou o perfil.
    set_user_profile_completed(db, uid, False)

    # Página: redireciona para /onboarding.
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert resp.headers.get("Location", "").endswith("/onboarding")

    # API: bloqueada com 403 e código específico.
    resp = client.get("/api/summary")
    assert resp.status_code == 403
    body = resp.get_json()
    assert body.get("code") == "ONBOARDING_REQUIRED"

    # Página de onboarding em si permanece acessível (GET).
    assert client.get("/onboarding").status_code == 200


def test_completing_onboarding_unblocks_user(
    client, register_user, login, db, user_id_by_email
):
    """Após POST /onboarding, o usuário fica liberado para páginas e APIs."""
    email = "onboard_finish@example.com"
    uid = _register_login(register_user, login, user_id_by_email, email)
    set_user_profile_completed(db, uid, False)

    resp = client.post(
        "/onboarding",
        data={
            "job_title": "Analista de Sistemas",
            "admission_date": "2026-01-05",
            "contract_type": "MENSALISTA",
            "base_rate": "10000",
            "monthly_hours": "220",
            "irrf_dependents": "0",
            "fixed_benefits_deduction": "0",
        },
    )
    # Sucesso: redireciona para a casa do usuário (/dashboard).
    assert resp.status_code == 302
    assert resp.headers.get("Location", "").endswith("/dashboard")

    # Perfil marcado como concluído e acessos liberados.
    from models.user import get_user

    assert get_user(db, uid).profile_completed is True
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/summary").status_code == 200
