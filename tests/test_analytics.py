"""
test_analytics.py
-----------------
Testes unitários das regras de cálculo financeiro:

  * INSS e IRRF progressivos (tabelas 2025).
  * Lógica progressiva de horas extras (70% até 30h, 100% acima).
  * Agregação do Líquido Recorrente (Recurrent Net Pay).
"""
import pytest

from models.profile import UserProfile
from services import analytics_service as a


# ---------------------------------------------------------------------
# INSS progressivo 2025
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "gross,expected",
    [
        (0.0, 0.0),
        (1000.0, 75.0),
        (2000.0, 157.23),
        (5000.0, 509.59),
    ],
)
def test_progressive_inss_2025(gross, expected):
    assert a._progressive_inss(gross) == pytest.approx(expected, abs=1e-2)


# ---------------------------------------------------------------------
# IRRF progressivo 2025
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "taxable,dependents,expected",
    [
        (2000.0, 0, 0.0),
        (3000.0, 0, 68.56),
        (5000.0, 0, 479.0),
        (3000.0, 1, 41.34),
    ],
)
def test_progressive_irrf_2025(taxable, dependents, expected):
    assert a._progressive_irrf(taxable, dependents) == pytest.approx(
        expected, abs=1e-2
    )


# ---------------------------------------------------------------------
# Líquido teórico recorrente (gross - INSS - IRRF - fixos)
# ---------------------------------------------------------------------
def test_theoretical_recurrent_net_horista():
    profile = UserProfile(
        user_id=1,
        contract_type="HORISTA",
        base_rate=10.0,
        monthly_hours=220,
        irrf_dependents=0,
        fixed_benefits_deduction=0.0,
    )
    gross = 10.0 * 220.0  # 2200.0
    inss = a._progressive_inss(gross)
    expected = gross - inss - a._progressive_irrf(gross - inss)
    assert a.theoretical_recurrent_net(profile) == pytest.approx(expected)


def test_theoretical_recurrent_net_mensalista():
    profile = UserProfile(
        user_id=1,
        contract_type="MENSALISTA",
        base_rate=3000.0,
        irrf_dependents=1,
        fixed_benefits_deduction=50.0,
    )
    gross = 3000.0
    inss = a._progressive_inss(gross)
    irrf = a._progressive_irrf(gross - inss, 1)
    expected = max(gross - inss - irrf - 50.0, 0.0)
    assert a.theoretical_recurrent_net(profile) == pytest.approx(expected)


# ---------------------------------------------------------------------
# Horas extras progressivas (70% até 30h, 100% acima)
# ---------------------------------------------------------------------
@pytest.fixture()
def overtime_profile():
    return UserProfile(
        user_id=1,
        contract_type="HORISTA",
        base_rate=10.0,            # R$ 10/hora
        monthly_hours=220,
        overtime_tier1_rate=1.70,  # +70% até o limite
        overtime_tier1_limit=30.0,
        overtime_tier2_rate=2.00,  # +100% acima do limite
        irrf_dependents=0,
        fixed_benefits_deduction=0.0,
    )


def test_overtime_within_tier1(overtime_profile):
    res = a.calculate_overtime_impact(overtime_profile, extra_hours=10.0)
    assert res["tier1_hours"] == 10.0
    assert res["tier2_hours"] == 0.0
    assert res["tier1_gross"] == pytest.approx(10 * 10.0 * 1.70)  # 170
    assert res["tier2_gross"] == pytest.approx(0.0)
    assert res["gross_extra"] == pytest.approx(170.0)


def test_overtime_tiered_split(overtime_profile):
    # 40h: 30h no 1º patamar (1.70) + 10h no 2º (2.00).
    res = a.calculate_overtime_impact(overtime_profile, extra_hours=40.0)
    assert res["tier1_hours"] == 30.0
    assert res["tier2_hours"] == 10.0
    assert res["tier1_gross"] == pytest.approx(30 * 10.0 * 1.70)  # 510
    assert res["tier2_gross"] == pytest.approx(10 * 10.0 * 2.00)  # 200
    assert res["gross_extra"] == pytest.approx(710.0)
    assert res["hourly_rate"] == pytest.approx(10.0)
    assert res["tier1_rate"] == pytest.approx(1.70)
    assert res["tier2_rate"] == pytest.approx(2.00)
    assert res["tier1_limit"] == pytest.approx(30.0)


def test_overtime_no_profile():
    res = a.calculate_overtime_impact(None, extra_hours=10.0)
    assert res["gross_extra"] == 0.0
    assert res["net_take_home"] == 0.0


# ---------------------------------------------------------------------
# Agregação do Líquido Recorrente (Recurrent Net Pay)
# ---------------------------------------------------------------------
def test_recurrent_net_pay_aggregation(db, register_user, login, seed_paystub):
    register_user(email="ana@example.com")
    login(email="ana@example.com")

    # 3 competências completas (não é o mês atual nem férias/admissão).
    seed_paystub("ana@example.com", "2024-01", net=1000.0)
    seed_paystub("ana@example.com", "2024-02", net=2000.0)
    seed_paystub("ana@example.com", "2024-03", net=3000.0)

    from models.user import get_user_by_email

    user_id = get_user_by_email(db, "ana@example.com").id
    meta = a.get_advanced_analytics(user_id)["meta"]

    assert meta["recurrent_net_months"] == 3
    assert meta["recurrent_net_sum"] == pytest.approx(6000.0)
    assert meta["recurrent_net_pay"] == pytest.approx(2000.0)


def test_recurrent_net_pay_excludes_incomplete_months(
    app, db, register_user, login, seed_paystub
):
    register_user(email="bia@example.com")
    login(email="bia@example.com")

    # 2 competências completas.
    seed_paystub("bia@example.com", "2024-01", net=1000.0)
    seed_paystub("bia@example.com", "2024-02", net=2000.0)

    # Mês de férias (parcial) que deve ser excluído do divisor.
    from database.connection import get_db

    with app.app_context():
        user_id = get_db().execute(
            "SELECT id FROM users WHERE email = ?", ["bia@example.com"]
        ).fetchone()["id"]
    totals = (
        '{"base_salary": 0, "total_earnings": 0, '
        '"total_deductions": 0, "net_value": 0}'
    )
    db.execute(
        "INSERT INTO holerites "
        "(user_id, company_name, mes_referencia, tipo_documento, totals) "
        "VALUES (?, 'Empresa', '2024-03', 'FERIAS', ?)",
        [user_id, totals],
    )
    db.commit()

    meta = a.get_advanced_analytics(user_id)["meta"]
    assert meta["recurrent_net_months"] == 2
    assert meta["recurrent_net_sum"] == pytest.approx(3000.0)
    assert meta["recurrent_net_pay"] == pytest.approx(1500.0)

