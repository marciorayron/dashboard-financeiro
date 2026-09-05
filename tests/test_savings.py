"""
tests/test_savings.py
---------------------
Testes da regra aprovada de arredondamento em centavos do "Planejador de
Poupança" (Planejador no `dashboard.js`, oráculo Python em
`services/savings_service.py`).

Regra: `baseSavings` e `bonus` são arredondados estritamente a 2 casas ANTES
de serem somados em `total` — garantindo que o Total coincida com a soma dos
valores formatados na UI (sem desvios de 1 centavo).
"""
import pytest

from services.savings_service import compute_savings_plan, round2


def test_round2_deterministic_cent():
    """round2 arredonda deterministicamente para centavos (estilo Math.round)."""
    assert round2(3277.0430) == pytest.approx(3277.04)
    assert round2(1122.9125) == pytest.approx(1122.91)
    assert round2(4399.9555) == pytest.approx(4399.96)
    assert round2(0.0) == 0.0


def test_savings_total_equals_sum_of_displayed_components():
    """
    Caso real do bug: as parcelas exibidas (R$ 3.277,04 + R$ 1.122,91) agora
    somam exatamente R$ 4.399,95 no Total (antes dava 4.399,96 por somar
    valores NÃO arredondados antes de formatar o total).
    """
    plan = compute_savings_plan(
        monthly_net=6554.0860,   # -> depósito mensal raw 3277,0430
        rate_pct=50,
        months=1,
        thirteenth=1500.0,
        vacation=745.825,        # (1500 + 745,825) × 50% -> bonus raw 1122,9125
        include_bonus=True,
    )

    assert plan["monthly"] == pytest.approx(3277.04)
    assert plan["base_savings"] == pytest.approx(3277.04)
    assert plan["bonus"] == pytest.approx(1122.91)
    # Total = arredonda(baseSavings) + arredonda(bonus) — SEM desvio de 1 centavo.
    assert plan["total"] == pytest.approx(4399.95)
    assert plan["total"] == pytest.approx(plan["base_savings"] + plan["bonus"])


def test_savings_exact_cent_summation_multiple_months():
    """Com N meses, base acumulada é arredondada e somada ao bônus exato."""
    plan = compute_savings_plan(
        monthly_net=6000.0,
        rate_pct=10,
        months=3,
        thirteenth=500.0,
        vacation=400.0,
        include_bonus=True,
    )
    assert plan["monthly"] == pytest.approx(600.0)          # 6000 × 10%
    assert plan["base_savings"] == pytest.approx(1800.0)    # 600 × 3
    assert plan["bonus"] == pytest.approx(90.0)             # (500+400) × 10%
    assert plan["total"] == pytest.approx(1890.0)
    assert plan["total"] == pytest.approx(plan["base_savings"] + plan["bonus"])


@pytest.mark.parametrize(
    "monthly_net,rate,months,thirteenth,vacation",
    [
        (3277.04, 20, 1, 3000.00, 1000.00),
        (5000.12, 15, 4, 4500.00, 1500.00),
        (1234.56, 25, 2, 300.00, 100.00),
        (8999.99, 30, 6, 8000.00, 2666.66),
    ],
)
def test_savings_total_always_matches_rounded_components(
    monthly_net, rate, months, thirteenth, vacation
):
    plan = compute_savings_plan(
        monthly_net=monthly_net,
        rate_pct=rate,
        months=months,
        thirteenth=thirteenth,
        vacation=vacation,
        include_bonus=True,
    )
    # Total é exatamente a soma dos componentes já em centavos.
    assert plan["total"] == pytest.approx(plan["base_savings"] + plan["bonus"], abs=1e-9)
    # Cada componente tem no máximo 2 casas decimais.
    for v in (plan["monthly"], plan["base_savings"], plan["bonus"], plan["total"]):
        assert round(v * 100, 6).is_integer()
