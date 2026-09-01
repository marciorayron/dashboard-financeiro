"""
tests/test_multi_tenant.py
---------------------------
Testes de aceitação para a expansão multi-tenant:

  1. RBAC: usuário não-admin recebe 403 ao acessar `/admin` (página e API).
  2. Profile Revision History: editar o perfil grava snapshot com vigência.
  3. Cálculo histórico: paystubs de competências passadas usam a taxa de
     pagamento ATIVA naquela data (não a taxa atual do perfil).

Usa `unittest` (stdlib) + test client do Flask com banco em arquivo
temporário para garantir isolamento entre testes.
"""
import os
import tempfile
import unittest

from app import create_app
from database.connection import get_db, init_db
from models.profile import (
    load_profile_history,
    resolve_profile_for_date,
)
from models.user import get_user_by_email, set_user_role
from services import analytics_service
from services.db_service import init_rubrica_catalog
from services.settings_service import save_tax_settings


def _register(client, name, email, password):
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


class MultiTenantBase(unittest.TestCase):
    """Base que cria app + banco temporário e dois usuários (admin/user)."""

    ADMIN_EMAIL = "admin@example.com"
    USER_EMAIL = "user@example.com"
    PASSWORD = "senha123"

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

        self.app = create_app("testing")
        self.app.config["TESTING"] = True
        self.app.config["DATABASE_PATH"] = self.db_path
        # Garante o schema no banco em arquivo (não no :memory: da criação).
        with self.app.app_context():
            init_db()
            init_rubrica_catalog(get_db())

        self.client = self.app.test_client()

        # Cria os usuários (ambos começam como 'user').
        _register(self.client, "Admin Principal", self.ADMIN_EMAIL, self.PASSWORD)
        _register(self.client, "Usuário Comum", self.USER_EMAIL, self.PASSWORD)

        # Promove o admin.
        with self.app.app_context():
            db = get_db()
            admin = get_user_by_email(db, self.ADMIN_EMAIL)
            set_user_role(db, admin.id, "admin")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _login(self, email):
        return self.client.post(
            "/login",
            data={"email": email, "password": self.PASSWORD},
            follow_redirects=True,
        )

    def _user_id(self, email):
        with self.app.app_context():
            db = get_db()
            u = get_user_by_email(db, email)
            return u.id


# ---------------------------------------------------------------------
# 1) RBAC / admin_required
# ---------------------------------------------------------------------
class AdminAccessTest(MultiTenantBase):
    def test_non_admin_gets_403_on_admin_page(self):
        self._login(self.USER_EMAIL)
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_gets_403_on_admin_api(self):
        self._login(self.USER_EMAIL)
        resp = self.client.get("/admin/api/metrics")
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_access_admin_page(self):
        self._login(self.ADMIN_EMAIL)
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_access_admin_api(self):
        self._login(self.ADMIN_EMAIL)
        resp = self.client.get("/admin/api/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("total_users", data)

    def test_unauthenticated_redirects_to_login(self):
        # Sem sessão: redireciona para o login (não é 403).
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 302)


# ---------------------------------------------------------------------
# 2) Profile Revision History
# ---------------------------------------------------------------------
class ProfileHistoryTest(MultiTenantBase):
    FULL_PROFILE = {
        "admission_date": "2023-01-01",
        "job_title": "Operador de Produção",
        "contract_type": "HORISTA",
        "base_rate": 15.0,
        "monthly_hours": 220,
        "irrf_dependents": 0,
        "fixed_benefits_deduction": 0.0,
    }

    def test_initial_save_records_first_snapshot(self):
        self._login(self.USER_EMAIL)
        resp = self.client.post("/api/profile", json=self.FULL_PROFILE)
        self.assertEqual(resp.status_code, 200)

        user_id = self._user_id(self.USER_EMAIL)
        with self.app.app_context():
            db = get_db()
            hist = load_profile_history(db, user_id)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].effective_date, "2023-01-01")
        self.assertAlmostEqual(hist[0].base_rate, 15.0)
        self.assertEqual(hist[0].job_title, "Operador de Produção")

    def test_edit_rate_creates_timestamped_snapshot(self):
        self._login(self.USER_EMAIL)
        self.client.post("/api/profile", json=self.FULL_PROFILE)

        # Altera apenas a taxa.
        changed = dict(self.FULL_PROFILE, base_rate=22.0)
        resp = self.client.post("/api/profile", json=changed)
        self.assertEqual(resp.status_code, 200)

        user_id = self._user_id(self.USER_EMAIL)
        with self.app.app_context():
            db = get_db()
            hist = load_profile_history(db, user_id)

        self.assertEqual(len(hist), 2)
        # Snapshot mais antigo = taxa antiga; mais recente = taxa nova.
        rates = sorted(h.base_rate for h in hist)
        self.assertEqual(rates, [15.0, 22.0])
        # Ambas as revisões possuem data de vigência preenchida.
        self.assertTrue(all(h.effective_date for h in hist))

    def test_no_history_on_unchanged_save(self):
        self._login(self.USER_EMAIL)
        self.client.post("/api/profile", json=self.FULL_PROFILE)
        # Salvar os mesmos dados não deve criar nova revisão.
        self.client.post("/api/profile", json=dict(self.FULL_PROFILE))

        user_id = self._user_id(self.USER_EMAIL)
        with self.app.app_context():
            db = get_db()
            hist = load_profile_history(db, user_id)
        self.assertEqual(len(hist), 1)



# ---------------------------------------------------------------------
# 3) Analytics histórico: taxa ativa por competência
# ---------------------------------------------------------------------
class HistoricalRateTest(MultiTenantBase):
    def _setup_profile(self, rate):
        self._login(self.USER_EMAIL)
        self.client.post(
            "/api/profile",
            json={
                "admission_date": "2023-01-01",
                "job_title": "Operador",
                "contract_type": "HORISTA",
                "base_rate": rate,
                "monthly_hours": 220,
                "irrf_dependents": 0,
                "fixed_benefits_deduction": 0.0,
            },
        )

    def test_past_paystub_uses_historical_rate(self):
        import datetime

        self._login(self.USER_EMAIL)
        # 1) Perfil inicial com taxa 15 (vigência = admissão 2023-01-01).
        self._setup_profile(15.0)
        # 2) Aumento de taxa para 40 hoje (vigência = hoje).
        self._setup_profile(40.0)

        user_id = self._user_id(self.USER_EMAIL)
        with self.app.app_context():
            db = get_db()

            # A competência passada (2024-06) deve usar a taxa de 15.
            p_old = resolve_profile_for_date(db, user_id, "2024-06-28")
            self.assertAlmostEqual(p_old.base_rate, 15.0)
            net_2024 = analytics_service.theoretical_recurrent_net_for_month(
                db, user_id, "2024-06"
            )

            # A partir de hoje (após a mudança) a taxa ativa é 40.
            p_now = resolve_profile_for_date(
                db, user_id, datetime.date.today().isoformat()
            )
            self.assertAlmostEqual(p_now.base_rate, 40.0)
            net_now = analytics_service.theoretical_recurrent_net(p_now, db)

            # Taxas diferentes -> líquidos diferentes; o passado reflete a
            # taxa antiga (15), não a atual (40).
            self.assertNotAlmostEqual(net_2024, net_now, delta=1.0)
            self.assertLess(net_2024, net_now)

    def test_tax_bracket_override_affects_analytics(self):
        self._login(self.USER_EMAIL)
        self._setup_profile(5000.0)

        user_id = self._user_id(self.USER_EMAIL)
        with self.app.app_context():
            db = get_db()
            net_before = analytics_service.theoretical_recurrent_net_for_month(
                db, user_id, "2026-08"
            )
            # Sobrescreve INSS (14% fixo) e IRRF (27,5% fixo) para elevar a
            # carga tributária => líquido teórico deve cair.
            save_tax_settings(
                db,
                {
                    "irrf": [[float("inf"), 0.275, 0.00]],
                    "inss": [[float("inf"), 0.14, 0.00]],
                    "dependent_deduction": 0.0,
                },
            )
            net_after = analytics_service.theoretical_recurrent_net_for_month(
                db, user_id, "2026-08"
            )
            self.assertLess(net_after, net_before)


if __name__ == "__main__":
    unittest.main()

