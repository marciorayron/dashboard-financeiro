"""
services/savings_service.py
----------------------------
Regra de negócio aprovada do "Planejador de Poupança" (arredondamento em centavos).

Contexto: a UI (Planejador de Poupança no `dashboard.js`) exibia as parcelas
(Base acumulada e Bônus 13º+Férias) arredondadas individualmente e o Total
somava os valores NÃO arredondados, causando desvios de 1 centavo (ex.:
R$ 3.277,04 + R$ 1.122,91 = R$ 4.399,96, quando deveria ser R$ 4.399,95).

Regra aprovada:
  * `baseSavings` e `bonus` são arredondados estritamente a 2 casas ANTES de
    serem somados;
  * `total = round2(baseSavings) + round2(bonus)` (== soma dos exibidos).

Este módulo é o **oráculo Python** (unit-testável) da mesma lógica que o
`dashboard.js` implementa em `savingsPlanCalc` (função `round2` no frontend).
"""
import math

__all__ = ["round2", "compute_savings_plan"]


def round2(value: float) -> float:
    """
    Arredonda para 2 casas decimais de forma DETERMINÍSTICA em centavos.

    Espelha `Math.round(val * 100) / 100` do JS (metade arredonda para cima
    em valores positivos): `floor(x*100 + 0.5) / 100`. Evita o arredondamento
    *banker's* (round-half-even) nativo do Python, que divergiria do navegador.
    """
    return math.floor(float(value or 0.0) * 100.0 + 0.5) / 100.0


def compute_savings_plan(
    monthly_net: float,
    rate_pct: float,
    months: int,
    thirteenth: float = 0.0,
    vacation: float = 0.0,
    include_bonus: bool = True,
) -> dict:
    """
    Calcula o plano de poupança com arredondamento estrito em centavos.

    Args:
        monthly_net (float): líquido mensal base (perfil/histórico).
        rate_pct (float): taxa de poupança em % (ex.: 20 = 20%).
        months (int): número de meses no período alvo (>= 1).
        thirteenth (float): 13º líquido anual.
        vacation (float): férias (1/3) líquidas anuais.
        include_bonus (bool): inclui o bônus de 13º + férias (taxa 1x).

    Returns:
        dict {"monthly", "base_savings", "bonus", "total", "months"} com
        valores em R$ com 2 casas (total == base_savings + bonus exato).
    """
    months = max(int(months or 0), 1)
    monthly_raw = float(monthly_net or 0.0) * float(rate_pct or 0.0) / 100.0
    monthly = round2(monthly_raw)

    # Acumulado dos depósitos mensais (cada depósito arredondado só no fim do
    # produto — baseSavings é o intermediário arredondado ANTES da soma).
    base_savings = round2(monthly_raw * months)

    bonus = 0.0
    if include_bonus:
        bonus_raw = (float(thirteenth or 0.0) + float(vacation or 0.0)) * (
            float(rate_pct or 0.0) / 100.0
        )
        bonus = round2(bonus_raw)

    total = round2(base_savings + bonus)
    return {
        "monthly": monthly,
        "base_savings": base_savings,
        "bonus": bonus,
        "total": total,
        "months": months,
    }
