"""
tests/test_seed.py
------------------
Testes do seeder automático de administrador (RBAC):

  1. O primeiro startup provisiona o admin padrão (admin@admin.com).
  2. O seeder é idempotente (segunda execução sem --reset não duplica).
  3. `seed_default_admin(force=True)` recria/reseta a conta padrão.
  4. O comando CLI `flask seed-admin` está registrado.
"""
from database.connection import get_db
from database.seed import seed_default_admin
from models.user import get_user_by_email


def test_init_seeds_default_admin(app):
    with app.app_context():
        db = get_db()
        admin = get_user_by_email(db, "admin@admin.com")
        assert admin is not None
        assert admin.is_admin
        assert admin.is_active


def test_seed_is_idempotent(app):
    with app.app_context():
        db = get_db()
        info = seed_default_admin(db, force=False)
        assert info["skipped"] is True
        assert info["created"] is False
        # Apenas um admin padrão permanece (sem duplicação por e-mail).
        rows = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE email = 'admin@admin.com'"
        ).fetchone()["c"]
        assert rows == 1


def test_seed_force_resets_default_admin(app):
    with app.app_context():
        db = get_db()
        info = seed_default_admin(db, force=True)
        assert info["skipped"] is False
        assert info["reset"] is True
        admin = get_user_by_email(db, "admin@admin.com")
        assert admin.is_admin
        assert admin.is_active


def test_seed_admin_cli_registered(app):
    assert "seed-admin" in app.cli.commands
