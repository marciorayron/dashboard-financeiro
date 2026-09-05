"""
analytics_service.py
--------------------
Serviço responsável por cálculos e agregações para o dashboard.

As consultas são sempre filtradas por `user_id` (isolamento multi-tenant)
e o `user_id` vem da sessão autenticada.

Contrato de dados (JSON em `holerites.totals`):
    base_salary, total_earnings, total_deductions, net_value

KPIs derivados:
    * Total de Proventos (total_earnings)
    * Total de Descontos (total_deductions)
    * Salário Líquido    (net_value)
    * Alíquota Efetiva   (total_deductions / total_earnings * 100)

Analytics avançados (`get_advanced_analytics`):
    * Overtime          -> horas extras (códigos 1503/1507/1523/1600 etc.) e
                           DSR sobre extras (1703/1723), com ratio sobre o bruto.
    * Taxas Efetivas    -> INSS e IRRF sobre o total de proventos.
    * Base vs Variável  -> proporção do salário base sobre a renda total.
"""
import json
import math
import re
from typing import List, Optional

from database.connection import get_db


def _to_float(value) -> float:
    """Converte um valor SQLite para float com segurança."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_div(numerator, denominator, default: float = 0.0) -> float:
    """
    Divisão segura: retorna `default` quando o denominador é zero ou não-finito,
    e também quando o resultado seria não-finito. Nunca produz Infinity/NaN.
    """
    try:
        numerator = float(numerator or 0.0)
        denominator = float(denominator or 0.0)
    except (TypeError, ValueError):
        return default
    if denominator == 0.0 or not math.isfinite(denominator):
        return default
    result = numerator / denominator
    if not math.isfinite(result):
        return default
    return result


def _load_totals(row) -> dict:
    """Lê a coluna `totals` (JSON) de forma segura."""
    raw = row["totals"] if "totals" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def get_user_totals(user_id: int, mes_referencia: Optional[str] = None) -> dict:
    """
    Retorna os KPIs agregados de um usuário, opcionalmente por mês.

    Returns:
        dict: {count, total_earnings, total_deductions, net_value, tax_rate}
    """
    db = get_db()
    query = "SELECT totals FROM holerites WHERE user_id = ?"
    params: List = [user_id]

    if mes_referencia:
        query += " AND mes_referencia = ?"
        params.append(mes_referencia)

    rows = db.execute(query, params).fetchall()

    count = len(rows)
    total_earnings = 0.0
    total_deductions = 0.0
    net_value = 0.0

    for row in rows:
        totals = _load_totals(row)
        total_earnings += _to_float(totals.get("total_earnings"))
        total_deductions += _to_float(totals.get("total_deductions"))
        net_value += _to_float(totals.get("net_value"))

    # Alíquota efetiva = descontos / proventos * 100.
    tax_rate = _safe_div(total_deductions, total_earnings) * 100.0

    return {
        "count": count,
        "total_earnings": round(total_earnings, 2),
        "total_deductions": round(total_deductions, 2),
        "net_value": round(net_value, 2),
        "tax_rate": round(tax_rate, 2),
    }


def get_monthly_series(user_id: int, company_name: Optional[str] = None) -> List[dict]:
    """
    Retorna a série temporal mensal (bruto x líquido) do usuário.

    Returns:
        List[dict]: um item por mês com chaves mes, gross, net.
    """
    db = get_db()
    query = """
        SELECT mes_referencia, totals
        FROM holerites
        WHERE user_id = ? AND mes_referencia IS NOT NULL
    """
    params: List = [user_id]

    if company_name:
        query += " AND company_name = ?"
        params.append(company_name)

    query += " ORDER BY mes_referencia ASC"
    rows = db.execute(query, params).fetchall()

    series = {}
    for row in rows:
        totals = _load_totals(row)
        mes = row["mes_referencia"]
        item = series.setdefault(mes, {"mes": mes, "gross": 0.0, "net": 0.0})
        item["gross"] += _to_float(totals.get("total_earnings"))
        item["net"] += _to_float(totals.get("net_value"))

    return list(series.values())


# Rubricas de provisão/compensação interna de férias — NÃO são descontos
# reais de bolso (são lançamentos de ajuste do pagamento de férias).
_VACATION_CLEARING_RE = re.compile(
    r"f[ée]rias.*imp|abono.*imp|provis[aã]o\s*adia|provis[aã]o\s*cont",
    re.I,
)


def get_descontos_por_rubrica(user_id: int) -> List[dict]:
    """
    Retorna a soma de descontos agrupados por descrição da rubrica.

    Exclui /B02 (adiantamento antecipado) e rubricas de compensação de férias
    ('Férias - imp', 'Abono de férias imp', 'Parc/créd Provisão ADIA', etc.),
    que são ajustes internos e não custo de bolso real.

    Returns:
        List[dict]: [{descricao, total}]
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT descricao, SUM(valor) AS total
        FROM rubricas_holerite
        WHERE user_id = ?
          AND tipo = 'desconto'
          AND UPPER(REPLACE(codigo, '/', '')) != 'B02'
        GROUP BY descricao
        ORDER BY total DESC
        """,
        [user_id],
    ).fetchall()

    result = []
    for row in rows:
        if _VACATION_CLEARING_RE.search(row["descricao"] or ""):
            continue
        result.append(
            {
                "descricao": row["descricao"],
                "total": round(_to_float(row["total"]), 2),
            }
        )
    return result


def get_empresas(user_id: int) -> List[str]:
    """Lista as empresas distintas de um usuário (filtro do dashboard)."""
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT company_name FROM holerites WHERE user_id = ? ORDER BY company_name",
        [user_id],
    ).fetchall()
    return [row["company_name"] for row in rows]


# ---------------------------------------------------------------------
# Analytics avançados
# ---------------------------------------------------------------------
# Conjuntos de códigos de rubrica (normalizados, sem a barra '/') usados
# para calcular métricas avançadas a partir das rubricas itemizadas.
_OVERTIME_CODES = {"1503", "1506", "1507", "1521", "1523", "1600"}
_DSR_OVERTIME_CODES = {"1703", "1723"}
_INSS_CODES = {"314", "303"}   # /314 INSS, /303 Trib. INSS (13º)
_IRRF_CODES = {"401"}
# Descontos operacionais/benefícios (custo real de bolso; exclui /B02).
_OPERATIONAL_CODES = {
    "4500",  # Desc. Co-Part. Pl. Saúde
    "4518",  # Taxa Negocial
    "4599",  # Reversão Salarial
    "4600",  # Desc. Co-Part. Pl. Odonto
    "4601",  # Seguro de Vida
    "4613",  # Fretado
    "4621",  # Refeitório
}

# Subcategorias de 'Outros Proventos' (proventos que não são base, horas
# extras nem DSR de extras).
_FERIAS_PROVENTO_CODES = {
    "M200", "MZ00", "MZ02", "MZ10", "MZ12", "MZ20", "MZ22", "MZ26", "MZ27",
    "MC03", "MC04", "MC13", "MC14", "MC40", "MC42", "MC46", "MC47", "M389",
}
_NOTURNO_PROVENTO_CODES = {
    "1596", "1600", "1604", "1606", "1796", "1800", "1804", "1806",
}
_BENEFICIO_PROVENTO_CODES = {
    "1303", "1334", "1352", "1357", "1360", "1699",
}


# ---------------------------------------------------------------------
# Cálculo de INSS/IRRF progressivos (tabelas 2025)
# ---------------------------------------------------------------------
# Faixas de INSS/IRRF (padrão 2025) e dedução por dependente.
# Os valores vigentes podem ser sobrescritos pelo painel `/admin` e são
# persistidos em `app_settings` (ver `services/settings_service.py`). As
# funções progressivas aceitam `db` opcional para aplicar esses overrides.
# ---------------------------------------------------------------------
from services.settings_service import (  # noqa: E402
    DEFAULT_DEPENDENT_DEDUCTION,
    DEFAULT_INSS_BRACKETS,
    DEFAULT_IRRF_BRACKETS,
)

_INSS_2025 = [tuple(b) for b in DEFAULT_INSS_BRACKETS]
_IRRF_2025 = [tuple(b) for b in DEFAULT_IRRF_BRACKETS]
_DEPENDENT_DEDUCTION = DEFAULT_DEPENDENT_DEDUCTION


def _get_inss_brackets(db=None) -> list:
    """Faixas de INSS vigentes (settings do admin quando existirem)."""
    if db is not None:
        try:
            from services.settings_service import get_tax_settings

            t = get_tax_settings(db)
            if t.get("inss"):
                return [tuple(b) for b in t["inss"]]
        except Exception:  # pragma: no cover - robustez em tempo de execução
            pass
    return _INSS_2025


def _get_irrf_brackets(db=None) -> list:
    """Faixas de IRRF vigentes (settings do admin quando existirem)."""
    if db is not None:
        try:
            from services.settings_service import get_tax_settings

            t = get_tax_settings(db)
            if t.get("irrf"):
                return [tuple(b) for b in t["irrf"]]
        except Exception:  # pragma: no cover - robustez em tempo de execução
            pass
    return _IRRF_2025


def _get_dependent_deduction(db=None) -> float:
    """Dedução por dependente no IRRF vigente (settings do admin)."""
    if db is not None:
        try:
            from services.settings_service import get_tax_settings

            t = get_tax_settings(db)
            return float(t.get("dependent_deduction") or _DEPENDENT_DEDUCTION)
        except Exception:  # pragma: no cover - robustez em tempo de execução
            pass
    return _DEPENDENT_DEDUCTION


def _progressive_inss(gross: float, db=None) -> float:
    """INSS progressivo sobre o salário bruto mensal."""
    if gross <= 0:
        return 0.0
    brackets = _get_inss_brackets(db)
    # INSS tem TETO de salário de contribuição: acima da última faixa a base é
    # capada (a contribuição não cresce com o excedente e nunca regride).
    top_limit = brackets[-1][0]
    if top_limit != float("inf") and gross > top_limit:
        gross = top_limit

    prev_limit = 0.0
    for limit, rate, accum in brackets:
        if gross <= limit:
            return accum + (gross - prev_limit) * rate
        prev_limit = limit
    # Fallback de segurança (ex.: faixa final sem limite superior explícito).
    limit, rate, accum = brackets[-1]
    return accum + (gross - prev_limit) * rate


def _progressive_irrf(taxable: float, dependents: int = 0, db=None) -> float:
    """IRRF progressivo sobre a base (bruto - INSS), com dependentes."""
    dependent_ded = _get_dependent_deduction(db)
    taxable = max(taxable - dependents * dependent_ded, 0.0)
    for limit, rate, ded in _get_irrf_brackets(db):
        if taxable <= limit:
            return max(taxable * rate - ded, 0.0)
    return 0.0


def theoretical_recurrent_net(profile, db=None) -> float:
    """
    Salário líquido recorrente TEÓRICO a partir do perfil.

    Gross = base_rate * monthly_hours (horista) ou base_rate (mensalista).
    Net  = gross - INSS progressivo - IRRF progressivo - descontos fixos.

    Quando `db` é fornecido, as faixas de INSS/IRRF vigentes (editáveis no
    `/admin`) são aplicadas; caso contrário usa os padrões da legislação.
    """
    if profile is None or getattr(profile, "base_rate", 0.0) <= 0:
        return 0.0
    contract = str(getattr(profile, "contract_type", "") or "HORISTA").upper()
    if contract == "HORISTA":
        gross = profile.base_rate * getattr(profile, "monthly_hours", 220.0)
    else:
        gross = profile.base_rate
    inss = _progressive_inss(gross, db)
    taxable = gross - inss
    irrf = _progressive_irrf(taxable, int(getattr(profile, "irrf_dependents", 0) or 0), db)
    fixed = getattr(profile, "fixed_benefits_deduction", 0.0) or 0.0
    return max(gross - inss - irrf - fixed, 0.0)


def _mes_to_date(mes_referencia: str) -> Optional[str]:
    """
    Converte uma competência 'YYYY-MM' para uma data 'YYYY-MM-DD' dentro do mês.

    Usa o dia 28 (sempre válido em qualquer mês) como referência de corte,
    de modo que um snapshot com `effective_date` em qualquer dia do mês seja
    considerado vigente para aquele holerite.
    """
    if not mes_referencia:
        return None
    s = str(mes_referencia).strip()
    if len(s) >= 7 and s[4] == "-":
        try:
            year = int(s[:4])
            month = int(s[5:7])
            return f"{year:04d}-{month:02d}-28"
        except (TypeError, ValueError):
            return None
    return None


def resolve_profile_for_date(db, user_id: int, date_str: str):
    """
    Resolve o perfil (e a taxa de pagamento) ativo na data informada.

    Delega para `models.profile.resolve_profile_for_date`, que consulta o
    histórico de revisões e faz fallback para o perfil atual quando necessário.
    """
    from models.profile import resolve_profile_for_date as _resolve

    return _resolve(db, user_id, date_str)


def theoretical_recurrent_net_for_month(db, user_id: int, mes_referencia: str) -> float:
    """
    Líquido recorrente teórico para uma competência específica ('YYYY-MM').

    Resolve a taxa de pagamento ativa na data da competência (via histórico de
    revisões do perfil) e aplica as faixas de INSS/IRRF vigentes, em vez de usar
    sempre a taxa atual do perfil.
    """
    date_str = _mes_to_date(mes_referencia)
    if date_str is None:
        return 0.0
    profile = resolve_profile_for_date(db, user_id, date_str)
    return theoretical_recurrent_net(profile, db)


def _norm_codigo(codigo) -> str:
    """Normaliza o código de uma rubrica (remove barra/espaços, maiúsculas)."""
    return str(codigo or "").strip().lstrip("/").upper()


def _row_get(row, key):
    """Lê um valor de dict ou sqlite3.Row com segurança."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _is_inss(row) -> bool:
    """True se a rubrica for INSS (por código /314 ou descrição)."""
    return _norm_codigo(_row_get(row, "codigo")) in _INSS_CODES or (
        "INSS" in str(_row_get(row, "descricao") or "").upper()
    )


def _is_irrf(row) -> bool:
    """True se a rubrica for IRRF (por código /401 ou descrição)."""
    return _norm_codigo(_row_get(row, "codigo")) in _IRRF_CODES or (
        "IRRF" in str(_row_get(row, "descricao") or "").upper()
    )


def _is_overtime(row) -> bool:
    """True se a rubrica for horas extras (códigos de overtime)."""
    return _norm_codigo(_row_get(row, "codigo")) in _OVERTIME_CODES


def _is_dsr_overtime(row) -> bool:
    """True se a rubrica for DSR sobre horas extras (1703/1723)."""
    return _norm_codigo(_row_get(row, "codigo")) in _DSR_OVERTIME_CODES


def _is_operational(row) -> bool:
    """True se a rubrica for um desconto operacional/benefício (custo real)."""
    return _norm_codigo(_row_get(row, "codigo")) in _OPERATIONAL_CODES


def build_effective_hourly_views(
    recurrent_net_pay: float,
    extra_net: float,
    monthly_hours: float,
    extra_hours: float,
    period_months: int,
    contractual_hours_total: float,
) -> dict:
    """
    Salário-hora efetivo nas DUAS visões aprovadas, com janelas coerentes.

    Visão Mensal (R$/h média mensal):
        net_mensal  = recurrent_net_pay + (extra_net / period_months)
        horas_mensais = monthly_hours + (extra_hours / period_months)

    Acumulado do Período (R$/h sobre todo o período):
        net_periodo   = (recurrent_net_pay × period_months) + extra_net
        horas_periodo = contractual_hours_total + extra_hours_total

    `extra_net` e `extra_hours` representam o ACUMULADO do período (H.E. + DSR),
    portanto são distribuídos por `period_months` na visão mensal.
    """
    period = max(int(period_months or 0), 1)

    monthly_net = recurrent_net_pay + (extra_net / period)
    monthly_hours_total = monthly_hours + (extra_hours / period)
    period_net = (recurrent_net_pay * period) + extra_net
    period_hours_total = contractual_hours_total + extra_hours

    return {
        "period_months": period,
        "monthly": {
            "net": round(monthly_net, 2),
            "hours": round(monthly_hours_total, 2),
            "rate": round(
                _safe_div(monthly_net, monthly_hours_total), 4
            ),
        },
        "period": {
            "net": round(period_net, 2),
            "hours": round(period_hours_total, 2),
            "rate": round(
                _safe_div(period_net, period_hours_total), 4
            ),
        },
    }


def get_advanced_analytics(
    user_id: int,
    mes_referencia: Optional[str] = None,
    company_name: Optional[str] = None,
) -> dict:
    """
    Calcula métricas analíticas avançadas, já formatadas para Plotly.

    Métricas:
        * Overtime: soma das horas extras (1503/1507/1523/1600 etc.) e do DSR
          sobre extras (1703/1723); ratio sobre o total de proventos.
        * Taxas Efetivas: alíquota de INSS e de IRRF sobre o total de proventos.
        * Base vs Variável: proporção do salário base sobre a renda total.

    Args:
        user_id (int): usuário autenticado (isolamento multi-tenant).
        mes_referencia (str, opcional): filtra por mês "YYYY-MM".
        company_name (str, opcional): filtra por empregador.

    Returns:
        dict com seções `meta`, `overtime`, `tax_rates` e `base_vs_variable`,
        contendo arrays `labels`/`values` prontos para gráficos Plotly.
    """
    db = get_db()

    # 1) Totais agregados (denominador comum das métricas).
    query_totals = (
        "SELECT tipo_documento, mes_referencia, totals FROM holerites WHERE user_id = ?"
    )
    params_totals: List = [user_id]
    if mes_referencia:
        query_totals += " AND mes_referencia = ?"
        params_totals.append(mes_referencia)
    if company_name:
        query_totals += " AND company_name = ?"
        params_totals.append(company_name)

    rows_totals = db.execute(query_totals, params_totals).fetchall()

    count = len(rows_totals)
    total_earnings = 0.0
    total_deductions = 0.0
    base_salary = 0.0

    # Perfil do usuário (para o líquido teórico e exclusão de meses parciais).
    # Quando um mês de competência é filtrado, resolve a taxa de pagamento
    # ATIVA naquela data (histórico de revisões) em vez da taxa atual.
    from models.profile import load_profile

    profile = load_profile(db, user_id)
    if mes_referencia:
        profile = resolve_profile_for_date(db, user_id, _mes_to_date(mes_referencia) or "")
        if profile is None or getattr(profile, "base_rate", 0.0) <= 0:
            profile = load_profile(db, user_id)

    # Agregação do take-home REAL por mês: soma o net de TODOS os holerites do
    # mês (adiantamento /B01 + folha mensal com /B02), resultando no total
    # efetivamente recebido naquele mês.
    monthly_net: dict = {}
    month_types: dict = {}
    for row in rows_totals:
        totals = _load_totals(row)
        total_earnings += _to_float(totals.get("total_earnings"))
        total_deductions += _to_float(totals.get("total_deductions"))
        base_salary += _to_float(totals.get("base_salary"))
        month = str(row["mes_referencia"] or "")[:7]
        if not month:
            continue
        monthly_net[month] = monthly_net.get(month, 0.0) + _to_float(
            totals.get("net_value")
        )
        month_types.setdefault(month, set()).add(
            str(row["tipo_documento"] or "").upper()
        )

    # Exclui meses incompletos do divisor: mês atual/ativo e meses com férias.
    import datetime

    current_month = datetime.date.today().strftime("%Y-%m")
    admission_month = None
    if profile and profile.admission_date:
        try:
            admission_month = profile.admission_date[:7]
        except Exception:
            admission_month = None

    complete_months = []
    for month in sorted(monthly_net.keys()):
        if mes_referencia and month == mes_referencia:
            # Regra aprovada (filtro de mês único): o mês selecionado NUNCA é
            # tratado como parcial aqui — recurrent_net_pay = take-home real.
            complete_months.append(month)
            continue
        if month == current_month:
            continue  # mês atual (ainda incompleto)
        if "FERIAS" in month_types.get(month, set()):
            continue  # mês com férias (parcial)
        if admission_month and month <= admission_month:
            continue  # antes/na admissão (mês inicial parcial)
        complete_months.append(month)

    recurrent_net = sum(monthly_net[m] for m in complete_months)
    recurrent_count = len(complete_months)
    recurrent_net_avg = _safe_div(recurrent_net, recurrent_count)
    theoretical = theoretical_recurrent_net(profile, db)

    # 2) Rubricas itemizadas (para overtime, INSS e IRRF).
    query_rub = """
        SELECT r.codigo, r.descricao, r.tipo, r.valor
        FROM rubricas_holerite r
        JOIN holerites h ON h.id = r.holerite_id
        WHERE r.user_id = ?
    """
    params_rub: List = [user_id]
    if mes_referencia:
        query_rub += " AND h.mes_referencia = ?"
        params_rub.append(mes_referencia)
    if company_name:
        query_rub += " AND h.company_name = ?"
        params_rub.append(company_name)

    rows_rub = db.execute(query_rub, params_rub).fetchall()

    overtime_total = 0.0
    dsr_overtime_total = 0.0
    inss_amount = 0.0
    irrf_amount = 0.0
    operational_amount = 0.0
    # Subcategorias de 'Outros Proventos'.
    ferias_amount = 0.0
    noturno_amount = 0.0
    beneficios_amount = 0.0

    for row in rows_rub:
        valor = _to_float(row["valor"])
        tipo = str(row["tipo"] or "").lower()
        if _is_overtime(row) and tipo == "provento":
            overtime_total += valor
        if _is_dsr_overtime(row) and tipo == "provento":
            dsr_overtime_total += valor
        if _is_inss(row):
            inss_amount += abs(valor)
        if _is_irrf(row):
            irrf_amount += abs(valor)
        # Descontos operacionais (custo real de bolso). /B02 (adiantamento
        # antecipado) é EXCLUÍDO de todas as análises de desconto.
        if _is_operational(row) and tipo == "desconto":
            operational_amount += abs(valor)

        # Proventos que compõem 'Outros Proventos' (não são base/HE/DSR-HE).
        if tipo == "provento":
            norm = _norm_codigo(_row_get(row, "codigo"))
            if norm == "1000" or _is_overtime(row) or _is_dsr_overtime(row):
                continue
            if norm in _FERIAS_PROVENTO_CODES:
                ferias_amount += abs(valor)
            elif norm in _NOTURNO_PROVENTO_CODES:
                noturno_amount += abs(valor)
            elif norm in _BENEFICIO_PROVENTO_CODES:
                beneficios_amount += abs(valor)

    # 'Descontos Efetivos' = apenas custo real de bolso (impostos + operacionais).
    true_deductions = inss_amount + irrf_amount + operational_amount

    # 3) Métricas derivadas (todas sobre o total de proventos).
    effective_tax_rate = _safe_div(total_deductions, total_earnings) * 100.0
    effective_deductions_rate = _safe_div(true_deductions, total_earnings) * 100.0
    overtime_ratio = (
        _safe_div(overtime_total + dsr_overtime_total, total_earnings) * 100.0
    )
    inss_rate = _safe_div(inss_amount, total_earnings) * 100.0
    irrf_rate = _safe_div(irrf_amount, total_earnings) * 100.0

    base_ratio = _safe_div(base_salary, total_earnings) * 100.0
    variable_pay = max(total_earnings - base_salary, 0.0)
    variable_ratio = max(100.0 - base_ratio, 0.0) if total_earnings else 0.0

    other_earnings = max(
        total_earnings - base_salary - overtime_total - dsr_overtime_total, 0.0
    )
    # Subcategorias de 'Outros Proventos'.
    outros_residuais = max(
        other_earnings - ferias_amount - noturno_amount - beneficios_amount, 0.0
    )
    # Net da 'Distribuição do Salário Bruto' = Proventos - Descontos Efetivos
    # (exclui /B02, que é adiantamento antecipado e não custo de bolso).
    net_after_true = total_earnings - true_deductions
    net_value = total_earnings - total_deductions

    # Salário-hora efetivo: horas contratuais (perfil) + estimativa de horas
    # extras (equivalente em horas-base do bruto de H.E./DSR) e o líquido das
    # extras após a alíquota efetiva de retenção.
    monthly_hours = 220.0
    base_hourly = 0.0
    if profile is not None and getattr(profile, "base_rate", 0.0) > 0:
        monthly_hours = float(getattr(profile, "monthly_hours", 220.0) or 220.0)
        base_hourly, _baseline_gross = _profile_hourly_gross(profile)
    extra_gross = overtime_total + dsr_overtime_total
    extra_hours = _safe_div(extra_gross, base_hourly) if base_hourly > 0 else 0.0

    # Net das horas extras via IMPOSTO MARGINAL (não a alíquota média do mês).
    # Aplica o INSS/IRRF marginal de acrescentar o extra_gross à base do mês —
    # evita que picos artificiais de taxa média (ex.: 83%) colapsem o líquido.
    dependents = int(getattr(profile, "irrf_dependents", 0) or 0) if profile else 0
    if extra_gross > 0.0 and profile is not None:
        base_gross = max(total_earnings - extra_gross, 0.0)
        inss_base = _progressive_inss(base_gross, db)
        inss_all = _progressive_inss(total_earnings, db)
        irrf_base = _progressive_irrf(max(base_gross - inss_base, 0.0), dependents, db)
        irrf_all = _progressive_irrf(max(total_earnings - inss_all, 0.0), dependents, db)
        marginal_tax = max((inss_all - inss_base) + (irrf_all - irrf_base), 0.0)
        extra_net = max(extra_gross - marginal_tax, 0.0)
    else:
        extra_net = extra_gross

    # --- Jornada de Trabalho & Esforço (Work Hours & Effort Tracking) ---
    # Card 1: horas contratuais totais no período (monthly_hours * meses).
    # Card 2: horas extras trabalhadas no período (derivadas do bruto de H.E.).
    # Card 3: divisão do overtime em patamares (50/70% até o limite vs. 100%).
    period_months = len(monthly_net)
    # Mês único filtrado: horas contratuais = pró-rata REAL pago no mês
    # (base_salary recebida / valor-hora do perfil), em vez do teto fixo 220 h
    # em meses parciais ou de férias.
    if mes_referencia and period_months == 1 and base_hourly > 0.0:
        worked = _safe_div(base_salary, base_hourly)
        if 0.0 < worked <= monthly_hours:
            monthly_hours = worked
    contractual_hours_total = monthly_hours * period_months
    tier1_limit = float(getattr(profile, "overtime_tier1_limit", 30.0) or 0.0)
    tier1_rate = float(getattr(profile, "overtime_tier1_rate", 1.70) or 0.0)
    tier2_rate = float(getattr(profile, "overtime_tier2_rate", 2.00) or 0.0)
    extra_hours_total = extra_hours
    # Salário-hora efetivo em ambas as visões aprovadas (Mensal / Período).
    # Uses RAW values (sem os arredondamentos de exibição) p/ fórmulas exatas.
    effective_hourly = build_effective_hourly_views(
        recurrent_net_pay=recurrent_net_avg,
        extra_net=extra_net,
        monthly_hours=monthly_hours,
        extra_hours=extra_hours_total,
        period_months=period_months,
        contractual_hours_total=contractual_hours_total,
    )
    tier1_hours = min(extra_hours_total, tier1_limit)
    tier2_hours = max(extra_hours_total - tier1_limit, 0.0)
    if extra_hours_total > 0:
        tier1_pct = _safe_div(tier1_hours, extra_hours_total) * 100.0
        tier2_pct = _safe_div(tier2_hours, extra_hours_total) * 100.0
    else:
        tier1_pct = 0.0
        tier2_pct = 0.0
    work_hours = {
        "period_months": period_months,
        "contractual_hours_total": round(contractual_hours_total, 1),
        "monthly_hours": round(monthly_hours, 1),
        "extra_hours_total": round(extra_hours_total, 1),
        "overtime_split": {
            "tier1_label": f"{tier1_rate:.2f}x (50/70%)",
            "tier2_label": f"{tier2_rate:.2f}x (100%)",
            "tier1_rate": round(tier1_rate, 2),
            "tier2_rate": round(tier2_rate, 2),
            "tier1_limit": round(tier1_limit, 1),
            "tier1_hours": round(tier1_hours, 1),
            "tier2_hours": round(tier2_hours, 1),
            "tier1_pct": round(tier1_pct, 2),
            "tier2_pct": round(tier2_pct, 2),
        },
    }

    return {
        "meta": {
            "count": count,
            "total_earnings": round(total_earnings, 2),
            "total_deductions": round(total_deductions, 2),
            "net_value": round(net_value, 2),
            "base_salary": round(base_salary, 2),
            "effective_tax_rate": round(effective_tax_rate, 2),
            "effective_deductions": round(true_deductions, 2),
            "effective_deductions_rate": round(effective_deductions_rate, 2),
            "operational_deductions": round(operational_amount, 2),
            "recurrent_net_pay": round(recurrent_net_avg, 2),
            "recurrent_net_sum": round(recurrent_net, 2),
            "recurrent_net_months": recurrent_count,
            "theoretical_recurrent_net": round(theoretical, 2),
            "monthly_hours": round(monthly_hours, 2),
            "base_hourly": round(base_hourly, 2),
        },
        "overtime": {
            "total": round(overtime_total, 2),
            "dsr_overtime": round(dsr_overtime_total, 2),
            "ratio": round(overtime_ratio, 2),
            "net": round(extra_net, 2),
            "extra_hours": round(extra_hours, 2),
            "labels": [
                "Salário Base",
                "Horas Extras",
                "DSR sobre Extras",
                "Outros Proventos",
            ],
            "values": [
                round(base_salary, 2),
                round(overtime_total, 2),
                round(dsr_overtime_total, 2),
                round(other_earnings, 2),
            ],
            # Decomposição da fatia 'Outros Proventos'.
            "outros": {
                "total": round(other_earnings, 2),
                "labels": [
                    "Férias & 1/3",
                    "Adicional Noturno & DSR",
                    "Benefícios & Reembolsos",
                    "Outros Residuais",
                ],
                "values": [
                    round(ferias_amount, 2),
                    round(noturno_amount, 2),
                    round(beneficios_amount, 2),
                    round(outros_residuais, 2),
                ],
            },
        },
        "effective_hourly": effective_hourly,
        "work_hours": work_hours,
        "tax_rates": {
            "inss": {"amount": round(inss_amount, 2), "rate": round(inss_rate, 2)},
            "irrf": {"amount": round(irrf_amount, 2), "rate": round(irrf_rate, 2)},
            "operational": {
                "amount": round(operational_amount, 2),
                "rate": round(
                    _safe_div(operational_amount, total_earnings) * 100.0, 2
                ),
            },
            "labels": ["INSS", "IRRF", "Descontos Operacionais", "Líquido"],
            "values": [
                round(inss_amount, 2),
                round(irrf_amount, 2),
                round(operational_amount, 2),
                round(net_after_true, 2),
            ],
        },
        "base_vs_variable": {
            "base_salary": round(base_salary, 2),
            "variable_pay": round(variable_pay, 2),
            "base_ratio": round(base_ratio, 2),
            "variable_ratio": round(variable_ratio, 2),
            "labels": ["Salário Base", "Remuneração Variável"],
            "values": [round(base_salary, 2), round(variable_pay, 2)],
        },
    }



# ---------------------------------------------------------------------
# Simulador de Horas Extras & Impacto Tributário
# ---------------------------------------------------------------------
def _profile_hourly_gross(profile) -> tuple:
    """
    Retorna (hourly_rate, baseline_monthly_gross) a partir do perfil.
    Horista -> base_rate é o valor/hora; Mensalista -> base_rate / horas.
    """
    contract = str(getattr(profile, "contract_type", "") or "HORISTA").upper()
    hours = float(getattr(profile, "monthly_hours", 220.0) or 220.0)
    if contract == "HORISTA":
        hourly = float(getattr(profile, "base_rate", 0.0) or 0.0)
        baseline = hourly * hours
    else:
        monthly = float(getattr(profile, "base_rate", 0.0) or 0.0)
        hourly = monthly / max(hours, 1.0)
        baseline = monthly
    return hourly, baseline


def calculate_overtime_impact(
    profile,
    extra_hours: float,
    overtime_rate_multiplier: float = 1.5,
) -> dict:
    """
    Calcula o impacto marginal de horas extras (bruto, INSS, IRRF e líquido).

    Usa as tabelas progressivas 2025: a retenção marginal é a diferença entre
    o imposto com e sem as horas extras, sem alterar dados históricos.

    Args:
        profile (UserProfile): perfil do usuário.
        extra_hours (float): horas extras a simular.
        overtime_rate_multiplier (float): multiplicador da HE (padrão 1.5).

    Returns:
        dict com gross_extra, marginal_inss, marginal_irrf, tax_bite,
        tax_bite_pct, net_take_home, per_hour_net.
    """
    extra_hours = float(extra_hours or 0.0)
    if profile is None or getattr(profile, "base_rate", 0.0) <= 0:
        return {
            "extra_hours": round(extra_hours, 2),
            "gross_extra": 0.0,
            "marginal_inss": 0.0,
            "marginal_irrf": 0.0,
            "tax_bite": 0.0,
            "tax_bite_pct": 0.0,
            "net_take_home": 0.0,
            "per_hour_net": 0.0,
            "hourly_rate": 0.0,
        }
    dependents = int(getattr(profile, "irrf_dependents", 0) or 0)
    hourly, baseline_gross = _profile_hourly_gross(profile)

    # Regras progressivas de horas extras (do perfil, com defaults).
    tier1_rate = float(getattr(profile, "overtime_tier1_rate", 0) or 1.70)
    tier1_limit = float(getattr(profile, "overtime_tier1_limit", 0) or 30.0)
    tier2_rate = float(getattr(profile, "overtime_tier2_rate", 0) or 2.00)

    # Gross por patamar: até o limite usa tier1; acima, tier2.
    tier1_hours = min(extra_hours, tier1_limit)
    tier2_hours = max(extra_hours - tier1_limit, 0.0)
    tier1_gross = tier1_hours * hourly * tier1_rate
    tier2_gross = tier2_hours * hourly * tier2_rate
    extra_gross = tier1_gross + tier2_gross

    total_gross = baseline_gross + extra_gross
    inss_base = _progressive_inss(baseline_gross)
    inss_total = _progressive_inss(total_gross)
    marginal_inss = inss_total - inss_base

    irrf_base = _progressive_irrf(baseline_gross - inss_base, dependents)
    irrf_total = _progressive_irrf(total_gross - inss_total, dependents)
    marginal_irrf = irrf_total - irrf_base

    marginal_tax = marginal_inss + marginal_irrf
    net_take_home = extra_gross - marginal_tax
    per_hour_net = _safe_div(net_take_home, extra_hours)
    tax_bite_pct = _safe_div(marginal_tax, extra_gross) * 100.0

    p1 = int(round((tier1_rate - 1) * 100))
    p2 = int(round((tier2_rate - 1) * 100))

    return {
        "extra_hours": round(extra_hours, 2),
        "hourly_rate": round(hourly, 2),
        "baseline_gross": round(baseline_gross, 2),
        "gross_extra": round(extra_gross, 2),
        "tier1_rate": round(tier1_rate, 2),
        "tier1_limit": round(tier1_limit, 2),
        "tier2_rate": round(tier2_rate, 2),
        "tier1_hours": round(tier1_hours, 2),
        "tier2_hours": round(tier2_hours, 2),
        "tier1_gross": round(tier1_gross, 2),
        "tier2_gross": round(tier2_gross, 2),
        "multipliers_label": f"{p1}% até {int(tier1_limit)}h | {p2}% acima",
        "marginal_inss": round(marginal_inss, 2),
        "marginal_irrf": round(marginal_irrf, 2),
        "tax_bite": round(marginal_tax, 2),
        "tax_bite_pct": round(tax_bite_pct, 2),
        "net_take_home": round(net_take_home, 2),
        "per_hour_net": round(per_hour_net, 2),
    }



# ---------------------------------------------------------------------
# Auditoria de paystubs & detecção de anomalias
# ---------------------------------------------------------------------
_BENEFIT_AUDIT_CODES = {"4613", "4621", "4500"}  # Fretado, Refeitório, Saúde
_NIGHT_AUDIT_CODES = {"1596", "1600", "1604", "1606", "1796", "1800", "1804", "1806"}


def audit_paystub_anomalies(
    user_id: int,
    mes_referencia: Optional[str] = None,
    company_name: Optional[str] = None,
) -> List[dict]:
    """
    Varre os paystubs históricos contra o perfil e médias móveis de 3 meses.

    Detecta:
      * Surto de descontos de benefícios (>15% sobre a média de 3 meses).
      * Adicional noturno/DSR ausente (esperado em folha horista).
      * Mudança de faixa de imposto (alíquota efetiva de IRRF).

    Returns:
        List[dict]: anomalias [{severity, category, title, description, month}]
    """
    db = get_db()
    from models.profile import load_profile

    profile = load_profile(db, user_id)

    rows = db.execute(
        """
        SELECT h.mes_referencia, h.tipo_documento, r.codigo, r.tipo, r.valor
        FROM rubricas_holerite r
        JOIN holerites h ON h.id = r.holerite_id
        WHERE r.user_id = ?
        """ + (" AND h.company_name = ?" if company_name else "")
        + """
        ORDER BY h.mes_referencia ASC
        """,
        [user_id] + ([company_name] if company_name else []),
    ).fetchall()

    months: dict = {}
    for row in rows:
        month = str(row["mes_referencia"] or "")[:7]
        if not month:
            continue
        info = months.setdefault(
            month,
            {
                "benefits": {c: 0.0 for c in _BENEFIT_AUDIT_CODES},
                "night": False,
                "irrf": 0.0,
                "earnings": 0.0,
                "tipo": str(row["tipo_documento"] or "").upper(),
            },
        )
        cod = _norm_codigo(_row_get(row, "codigo"))
        valor = _to_float(row["valor"])
        tipo = str(row["tipo"]).lower()
        if cod in _BENEFIT_AUDIT_CODES and tipo == "desconto":
            info["benefits"][cod] += abs(valor)
        if cod in _NIGHT_AUDIT_CODES and tipo == "provento":
            info["night"] = True
        if cod in _IRRF_CODES:
            info["irrf"] += abs(valor)
        if tipo == "provento":
            info["earnings"] += abs(valor)

    ordered = sorted(months.keys())
    flags: List[dict] = []

    # Benefícios: compara cada mês com a média dos 3 anteriores.
    for i, month in enumerate(ordered):
        if i < 3:
            continue
        cur = months[month]["benefits"]
        prev_avg = {
            c: sum(months[ordered[j]]["benefits"][c] for j in range(i - 3, i)) / 3.0
            for c in _BENEFIT_AUDIT_CODES
        }
        for code in _BENEFIT_AUDIT_CODES:
            avg = prev_avg[code]
            if avg <= 0 or cur[code] <= 0:
                continue
            pct = _safe_div(cur[code] - avg, avg) * 100.0
            if pct > 15.0:
                label = {"4613": "Fretado", "4621": "Refeição", "4500": "Plano de Saúde"}.get(code, code)
                flags.append(
                    {
                        "severity": "HIGH" if pct > 40 else "MEDIUM",
                        "category": "beneficio",
                        "title": f"Surto de {label}",
                        "description": (
                            f"Desconto de {label} subiu {pct:.0f}% em {month} "
                            f"vs. média de 3 meses (R$ {cur[code]:.2f} vs. R$ {avg:.2f})."
                        ),
                        "month": month,
                        # Impacto monetário da discrepância (R$).
                        "amount": round(cur[code] - avg, 2),
                    }
                )

    # Adicional noturno ausente em folha horista.
    if profile and profile.contract_type == "HORISTA":
        present = [m for m in ordered if months[m]["night"]]
        if present:
            last_night = present[-1]
            for m in ordered:
                if m > last_night and "FERIAS" not in months[m]["tipo"]:
                    flags.append(
                        {
                            "severity": "MEDIUM",
                            "category": "adicional",
                            "title": "Adicional Noturno ausente",
                            "description": (
                                f"Nenhum adicional noturno/DSR noturno registrado em {m}, "
                                "embora presente em meses anteriores."
                            ),
                            "month": m,
                            "amount": 0.0,
                        }
                    )

    # Mudança de faixa de IRRF (alíquota efetiva).
    for i in range(1, len(ordered)):
        prev_r = _safe_div(months[ordered[i - 1]]["irrf"], months[ordered[i - 1]]["earnings"]) * 100.0
        cur_r = _safe_div(months[ordered[i]]["irrf"], months[ordered[i]]["earnings"]) * 100.0
        if cur_r > 0 and abs(cur_r - prev_r) > 5.0:
            flags.append(
                {
                    "severity": "HIGH",
                    "category": "imposto",
                    "title": "Mudança de faixa de IRRF",
                    "description": (
                        f"Alíquota efetiva de IRRF saltou de {prev_r:.1f}% para "
                        f"{cur_r:.1f}% entre {ordered[i-1]} e {ordered[i]}."
                    ),
                    "month": ordered[i],
                    "amount": 0.0,
                }
            )

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    flags.sort(key=lambda f: order.get(f["severity"], 9))

    # Escopo por competência: as médias móveis continuam usando o histórico,
    # mas apenas as anomalias DO mês solicitado são retornadas.
    if mes_referencia:
        flags = [f for f in flags if f.get("month") == mes_referencia]
    return flags



# ---------------------------------------------------------------------
# Projeção financeira anual & planejamento tributário
# ---------------------------------------------------------------------
def get_annual_financial_projection(profile, historical_data: Optional[dict] = None) -> dict:
    """
    Projeta o take-home anual do usuário.

    Inclui:
      * 12 meses de líquido recorrente (teórico do perfil ou média histórica).
      * 13º salário (1ª e 2ª parcelas com INSS/IRRF).
      * 1/3 constitucional de férias (com INSS/IRRF).
      * PPR/PLR estimado (média histórica, isento dentro do teto legal).

    Args:
        profile (UserProfile): perfil do usuário.
        historical_data (dict, opcional): {"monthly_net", "ppr_avg"}.

    Returns:
        dict com breakdown anual e picos sazonais.
    """
    historical_data = historical_data or {}
    if profile is None:
        return {"error": "Perfil não configurado."}
    dependents = int(getattr(profile, "irrf_dependents", 0) or 0)
    hourly, monthly_gross = _profile_hourly_gross(profile)

    monthly_net = float(historical_data.get("monthly_net") or 0.0) or theoretical_recurrent_net(profile)

    # 13º salário: base mensal bruta, parcelas em Nov e Dez (50% cada).
    inss_13 = _progressive_inss(monthly_gross)
    irrf_13 = _progressive_irrf(monthly_gross - inss_13, dependents)
    net_13 = max(monthly_gross - inss_13 - irrf_13, 0.0)
    installment = net_13 / 2.0

    # 1/3 constitucional de férias.
    third = monthly_gross / 3.0
    inss_v = _progressive_inss(third)
    irrf_v = _progressive_irrf(third - inss_v, dependents)
    net_vacation = max(third - inss_v - irrf_v, 0.0)

    # PPR/PLR estimado (média histórica de folhas PPR).
    ppr_avg = float(historical_data.get("ppr_avg") or 0.0)

    baseline_annual = monthly_net * 12
    total = baseline_annual + net_13 + net_vacation + ppr_avg

    return {
        "monthly_gross": round(monthly_gross, 2),
        "monthly_net": round(monthly_net, 2),
        "baseline_annual": round(baseline_annual, 2),
        "thirteenth": {
            "gross": round(monthly_gross, 2),
            "net": round(net_13, 2),
            "first_installment": round(installment, 2),
            "second_installment": round(installment, 2),
        },
        "vacation_bonus": {
            "gross": round(third, 2),
            "net": round(net_vacation, 2),
        },
        "ppr_estimate": round(ppr_avg, 2),
        "total_annual_take_home": round(total, 2),
        "seasonal_spikes": [
            {"label": "1ª parcela 13º", "month": "Novembro", "amount": round(installment, 2)},
            {"label": "2ª parcela 13º", "month": "Dezembro", "amount": round(installment, 2)},
            {"label": "Férias (1/3)", "month": "Férias", "amount": round(net_vacation, 2)},
            {"label": "PPR/PLR", "month": "Variável", "amount": round(ppr_avg, 2)},
        ],
    }



# ---------------------------------------------------------------------
# Projeção tributária anual (INSS & IRRF) — acumulado YTD + projetado
# ---------------------------------------------------------------------
def _monthly_retention_series(user_id: int, year: str) -> dict:
    """
    Retenções reais de INSS/IRRF por competência dentro de um ano.

    Returns:
        dict ordenado por mês: { 'YYYY-MM': {'inss': float, 'irrf': float} }
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT h.mes_referencia, r.codigo, r.descricao, r.valor
        FROM rubricas_holerite r
        JOIN holerites h ON h.id = r.holerite_id
        WHERE r.user_id = ? AND h.mes_referencia LIKE ?
        """,
        [user_id, year + "-%"],
    ).fetchall()

    monthly: dict = {}
    for row in rows:
        month = str(_row_get(row, "mes_referencia") or "")[:7]
        if not month:
            continue
        info = monthly.setdefault(month, {"inss": 0.0, "irrf": 0.0})
        valor = _to_float(_row_get(row, "valor"))
        if _is_inss(row):
            info["inss"] += abs(valor)
        elif _is_irrf(row):
            info["irrf"] += abs(valor)
    return dict(sorted(monthly.items()))


def _monthly_gross_series(user_id: int, year: str) -> dict:
    """
    Série mensal de SALÁRIO BRUTO (total_earnings) por competência no ano.

    Considera apenas holerites de tipo 'FOLHA_MENSAL' (exclui 13º/férias/PPR/
    adiantamento, que distorceriam o baseline recorrente). Quando uma mesma
    competência possui mais de um documento (ex.: fixtures com INSS/IRRF em
    folhas separadas), usa o MAIOR bruto do mês para evitar dupla contagem.

    Returns:
        dict ordenado por mês: { 'YYYY-MM': float(gross) }
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT mes_referencia, totals
        FROM holerites
        WHERE user_id = ? AND mes_referencia LIKE ?
              AND tipo_documento = 'FOLHA_MENSAL'
        """,
        [user_id, year + "-%"],
    ).fetchall()

    monthly: dict = {}
    for row in rows:
        month = str(_row_get(row, "mes_referencia") or "")[:7]
        if not month:
            continue
        gross = _to_float(_load_totals(row).get("total_earnings"))
        monthly[month] = max(monthly.get(month, 0.0), gross)
    return dict(sorted(monthly.items()))


def _monthly_gross_values(user_id: int, year: str) -> List[float]:
    """Lista ordenada dos salários brutos mensais reais (>0) do ano."""
    return [v for v in _monthly_gross_series(user_id, year).values() if v > 0.0]


def _projected_monthly_gross(
    method: str, gross_series: List[float], profile_gross: float
) -> float:
    """
    Salário bruto mensal projetado (baseline ÚNICO) conforme o método.

      * Com histórico: projeta a partir dos grosses reais (mesma heurística
        do `_project_rate`: run-rate / tendência 3M / média YTD).
      * Sem histórico: recorre ao gross teórico do perfil (anchor estável),
        para não zerar a projeção nem perder coerência.
    """
    if gross_series:
        return max(float(_project_rate(method, gross_series)), 0.0)
    return max(float(profile_gross or 0.0), 0.0)


def _projected_monthly_taxes(
    projected_gross: float, dependents: int, db=None
) -> tuple:
    """
    Calcula INSS e IRRF MENSais a partir de UM ÚNICO gross projetado.

    Garantias matemáticas:
      * INSS  = tabela progressiva oficial sobre projected_gross (capped no
                máximo mensal de contribuição legal).
      * IRRF  = tabela progressiva sobre a MESMA base, abatendo o INSS
                calculado acima: Base = gross - INSS - dedução por dependente.
    """
    raw_inss = _progressive_inss(projected_gross, db)
    max_monthly_inss = _inss_max_monthly_contribution(db)
    monthly_inss = (
        round(min(raw_inss, max_monthly_inss), 2)
        if max_monthly_inss > 0.0
        else round(raw_inss, 2)
    )
    taxable = max(projected_gross - monthly_inss, 0.0)
    monthly_irrf = round(_progressive_irrf(taxable, dependents, db), 2)
    return monthly_inss, monthly_irrf


def _project_rate(method: str, values: List[float]) -> float:
    """
    Taxa mensal projetada de retenção conforme o método escolhido.

      * last_month : usa o valor da competência mais recente (run-rate).
      * trend      : regressão linear sobre as últimas 3 observações,
                     projetando a competência seguinte (nunca negativa).
      * average    : média de todas as observações (YTD).

    Args:
        method (str): 'average', 'trend' ou 'last_month'.
        values (List[float]): série ordenada (mês a mês) de retenção.

    Returns:
        float: taxa mensal projetada (>= 0).
    """
    if not values:
        return 0.0
    if method == "last_month":
        return float(values[-1])
    if method == "trend":
        window = values[-3:] if len(values) >= 3 else values
        if len(window) < 2:
            return float(window[-1])
        n = len(window)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(window) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        slope = 0.0
        if denom:
            slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, window)) / denom
        intercept = mean_y - slope * mean_x
        return max(intercept + slope * n, 0.0)
    # average: média de todas as competências YTD.
    return sum(values) / len(values)


def _inss_max_monthly_contribution(db=None) -> float:
    """
    Contribuição mensal máxima de INSS (teto do salário de contribuição).

    Corresponde ao INSS progressivo aplicado ao teto da faixa mais alta —
    o valor máximo que um trabalhador paga em um único mês.
    """
    brackets = _get_inss_brackets(db)
    if not brackets:
        return 0.0
    top_limit = brackets[-1][0]
    return _progressive_inss(top_limit, db)


def _inss_annual_ceiling(db=None) -> float:
    """
    Teto anual de INSS (soma das contribuições máximas mensais do ano).

    Quando o INSS acumulado no ano (YTD) atinge esse teto, os meses seguintes
    passam a reter R$ 0,00 de INSS — motivo pelo qual run-rate/tendência podem
    "zerar" indevidamente se as retenções forem interpretadas sem considerar o
    teto.
    """
    return round(_inss_max_monthly_contribution(db) * 12, 2)



def get_tax_projection(user_id: int, method: str = "average") -> dict:
    """
    Projeção tributária anual acumulada (INSS & IRRF) com métodos dinâmicos.

    Calcula:
        * ytd: retenções REAIS de INSS e IRRF acumuladas no ano corrente.
        * monthly: SALÁRIO BRUTO mensal projetado (baseline ÚNICO pelo método)
          e os INSS/IRRF MENSais derivados desse mesmo gross pelas tabelas
          oficiais (IRRF abate o INSS calculado + dependentes).
        * series: retenções reais mês a mês (contexto/tendência, exibição).
        * projected: retenções projetadas para os meses restantes até dezembro.
        * annual: projeção anual total (real + projetado) e baseline líquido.

    Nota de coerência: INSS e IRRF nunca são extrapolados de séries históricas
    isoladas de retenção — ambos são recalculados a partir de um único gross
    projetado, impedindo pares matematicamente impossíveis (ex.: INSS alto com
    IRRF artificialmente baixo).

    Args:
        user_id (int): usuário autenticado (isolamento multi-tenant).
        method (str): 'average' (média YTD), 'trend' (regressão últimos 3M)
            ou 'last_month' (run-rate do último paystub).

    Returns:
        dict com seções `year`, `month_now`, `remaining_months`, `method`,
        `monthly`, `series`, `ytd`, `projected` e `annual`.
    """
    import datetime

    method = (method or "average").lower()
    if method not in ("average", "trend", "last_month"):
        method = "average"

    db = get_db()
    from models.profile import load_profile

    profile = load_profile(db, user_id)

    today = datetime.date.today()
    year = str(today.year)

    # Série mensal real de retenções no ano corrente.
    monthly_series = _monthly_retention_series(user_id, year)
    inss_series = [m["inss"] for m in monthly_series.values()]
    irrf_series = [m["irrf"] for m in monthly_series.values()]
    ytd_inss = sum(inss_series)
    ytd_irrf = sum(irrf_series)

    # Perfil (âncora teórica quando não há folha no ano e para exibição).
    dependents = int(getattr(profile, "irrf_dependents", 0) or 0) if profile else 0
    profile_gross = 0.0
    theoretical_inss = 0.0
    theoretical_irrf = 0.0
    if profile is not None:
        _, profile_gross = _profile_hourly_gross(profile)
        theoretical_inss = _progressive_inss(profile_gross, db)
        theoretical_irrf = _progressive_irrf(
            profile_gross - theoretical_inss, dependents, db
        )

    month_now = today.month
    remaining_months = max(12 - month_now, 0)

    # ------------------------------------------------------------------
    # 1) BASELINE ÚNICO — salário bruto mensal projetado pelo método.
    #    INSS e IRRF NUNCA são extrapolados de séries isoladas de retenção;
    #    ambos derivam DESTE projected_gross, usando as tabelas oficiais.
    # ------------------------------------------------------------------
    gross_series = _monthly_gross_values(user_id, year)
    projected_gross = _projected_monthly_gross(method, gross_series, profile_gross)

    # 2) INSS e IRRF mensais calculados do MESMO gross projetado:
    #      INSS    = tabela progressiva sobre projected_gross (capped mensal).
    #      Base IRRF = projected_gross - INSS_calculado - dedução por dependente.
    monthly_inss, monthly_irrf = _projected_monthly_taxes(
        projected_gross, dependents, db
    )
    monthly_net = round(max(projected_gross - monthly_inss - monthly_irrf, 0.0), 2)
    monthly_gross = round(projected_gross, 2)

    # 3) TETO ANUAL DE INSS: se inss_ytd + (INSS mensal × meses restantes)
    #    exceder o teto legal, a projeção INSS remanescente é limitada ao
    #    headroom restante (flag `inss_capped`). O IRRF mantém-se coerente.
    inss_ceiling = _inss_annual_ceiling(db)
    inss_capped = False
    wanted_inss = round(monthly_inss * remaining_months, 2)
    if inss_ceiling > 0:
        inss_remaining_headroom = max(round(inss_ceiling - ytd_inss, 2), 0.0)
        if wanted_inss > inss_remaining_headroom + 0.005:
            inss_capped = True
            projected_inss = inss_remaining_headroom
        else:
            projected_inss = wanted_inss
    else:
        projected_inss = wanted_inss
    projected_irrf = round(monthly_irrf * remaining_months, 2)

    notes = []
    if inss_capped:
        notes.append(
            "Teto do INSS anual alcançado: retenções adicionais limitadas "
            f"ao headroom legal (R$ {inss_remaining_headroom:,.2f})."
        )

    return {
        "year": year,
        "month_now": month_now,
        "remaining_months": remaining_months,
        "method": method,
        "inss_capped": inss_capped,
        "notes": notes,
        "monthly": {
            "gross": round(monthly_gross, 2),
            "net": round(monthly_net, 2),
            "inss": monthly_inss,
            "irrf": monthly_irrf,
            "theoretical_inss": round(theoretical_inss, 2),
            "theoretical_irrf": round(theoretical_irrf, 2),
            "inss_capped": inss_capped,
        },
        "series": {
            "months": list(monthly_series.keys()),
            "inss": [round(v, 2) for v in inss_series],
            "irrf": [round(v, 2) for v in irrf_series],
        },
        "ytd": {
            "inss": round(ytd_inss, 2),
            "irrf": round(ytd_irrf, 2),
        },
        "projected": {
            "inss": projected_inss,
            "irrf": projected_irrf,
        },
        "annual": {
            "inss": round(ytd_inss + projected_inss, 2),
            "irrf": round(ytd_irrf + projected_irrf, 2),
            "inss_ceiling": inss_ceiling,
            "net_baseline": round(monthly_net * 12, 2),
        },
    }



# ---------------------------------------------------------------------
# Inconsistências — breakdown itemizado do total monetário
# ---------------------------------------------------------------------
def get_inconsistency_breakdown(user_id: int) -> dict:
    """
    Quebra itemizada do total R$ das inconsistências detectadas.

    Retorna o total monetário e cada item, garantindo que todo item possua
    a chave `amount` (0.0 quando não é uma discrepância puramente monetária).

    Returns:
        dict: {"total", "count", "items": [{severity, category, title,
        description, month, amount}]}
    """
    flags = audit_paystub_anomalies(user_id)
    total = sum(float(f.get("amount") or 0.0) for f in flags)
    return {
        "total": round(total, 2),
        "count": len(flags),
        "items": flags,
    }


# ---------------------------------------------------------------------
# Contexto mensal para a IA (temporal / por competência)
# ---------------------------------------------------------------------
def build_ai_paystub_context(user_id: int) -> dict:
    """
    Constrói o contexto mensal (mês a mês) dos holerites do usuário para a IA.

    Lista as competências de SALÁRIO MENSAL (estritamente `FOLHA_MENSAL`,
    excluindo PPR/13º/adiantamento/férias) com bruto/líquido/INSS/IRRF, tags
    de categoria (`categoria`) e competência (`mes_referencia`), além dos
    agregados globais (YTD e médias). PPR/adiantamento nunca contam como base
    ou salário regular de um mês. Permite à IA responder perguntas temporais,
    ex.: "qual o menor e o maior salário e em quais meses?".

    Returns:
        dict: {"monthly": [...], "aggregates": {...}}
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT h.id, h.mes_referencia, h.totals
        FROM holerites h
        WHERE h.user_id = ?
              AND h.mes_referencia IS NOT NULL
              AND TRIM(h.mes_referencia) != ''
              AND UPPER(COALESCE(h.tipo_documento, '')) = 'FOLHA_MENSAL'
        ORDER BY h.mes_referencia ASC, h.id ASC
        """,
        [user_id],
    ).fetchall()

    # Rubricas/retensões restritas aos MESMOS documentos FOLHA_MENSAL (não
    # misturamos PPR/13º/adiantamento com o salário mensal da competência).
    rub_rows = db.execute(
        """
        SELECT h.id AS holerite_id, r.codigo, r.descricao, r.tipo, r.valor
        FROM rubricas_holerite r
        JOIN holerites h ON h.id = r.holerite_id
        WHERE r.user_id = ?
              AND UPPER(COALESCE(h.tipo_documento, '')) = 'FOLHA_MENSAL'
        ORDER BY h.mes_referencia ASC, h.id ASC
        """,
        [user_id],
    ).fetchall()

    taxes_by_doc: dict = {}
    rubrics_by_doc: dict = {}
    for r in rub_rows:
        doc_id = _row_get(r, "holerite_id")
        valor = _to_float(_row_get(r, "valor"))
        taxes = taxes_by_doc.setdefault(doc_id, {"inss": 0.0, "irrf": 0.0})
        if _is_inss(r):
            taxes["inss"] += abs(valor)
        elif _is_irrf(r):
            taxes["irrf"] += abs(valor)

        descricao = str(_row_get(r, "descricao") or "").strip()
        if descricao:
            bucket = rubrics_by_doc.setdefault(doc_id, {})
            bucket[descricao] = round(bucket.get(descricao, 0.0) + abs(valor), 2)

    # Um registro por competência (salário mensal real). Se a mesma competência
    # tiver mais de um FOLHA_MENSAL (ex.: INSS/IRRF em folha separada), usa o
    # documento de MAIOR bruto — evita dupla contagem, igual a _monthly_gross_series.
    best_by_month: dict = {}
    for row in rows:
        comp = str(_row_get(row, "mes_referencia") or "")[:7]
        if not comp:
            continue
        totals = _load_totals(row)
        gross = _to_float(totals.get("total_earnings"))
        cur = best_by_month.get(comp)
        if cur is None or gross > cur["gross"]:
            doc_id = row["id"]
            best_by_month[comp] = {
                "gross": gross,
                "net": _to_float(totals.get("net_value")),
                "inss": round(taxes_by_doc.get(doc_id, {}).get("inss", 0.0), 2),
                "irrf": round(taxes_by_doc.get(doc_id, {}).get("irrf", 0.0), 2),
                "rubrics": rubrics_by_doc.get(doc_id, {}),
            }

    monthly: List[dict] = []
    for comp in sorted(best_by_month.keys()):
        b = best_by_month[comp]
        monthly.append(
            {
                "competencia": comp,
                "mes_referencia": comp,
                "categoria": "FOLHA_MENSAL",
                "gross": round(b["gross"], 2),
                "net": round(b["net"], 2),
                "inss": b["inss"],
                "irrf": b["irrf"],
                "rubrics": b["rubrics"],
            }
        )

    if not monthly:
        return {"monthly": [], "aggregates": {}}

    grosses = [m["gross"] for m in monthly]
    nets = [m["net"] for m in monthly]
    aggregates = {
        "count": len(monthly),
        "ytd_gross": round(sum(grosses), 2),
        "ytd_net": round(sum(nets), 2),
        "ytd_inss": round(sum(m["inss"] for m in monthly), 2),
        "ytd_irrf": round(sum(m["irrf"] for m in monthly), 2),
        "avg_gross": round(sum(grosses) / len(grosses), 2),
        "avg_net": round(sum(nets) / len(nets), 2),
        "min_gross": round(min(grosses), 2),
        "max_gross": round(max(grosses), 2),
        "min_month": monthly[grosses.index(min(grosses))]["competencia"],
        "max_month": monthly[grosses.index(max(grosses))]["competencia"],
    }
    return {"monthly": monthly, "aggregates": aggregates}


# ---------------------------------------------------------------------
# Explicação de cards com IA (Explain ✨) — escopo por card/mês/empresa
# ---------------------------------------------------------------------
def _brl(value) -> str:
    """Formata valor como moeda pt-BR (R$ 1.234,56)."""
    try:
        return "R$ " + f"{float(value or 0.0):,.2f}".replace(
            ",", "§"
        ).replace(".", ",").replace("§", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"


# Títulos human-readable de cada card suportado pelo Explain ✨. A chave
# técnica (snake_case) NUNCA deve aparecer no markdown/erros — apenas o rótulo.
_CARD_TITLES = {
    "recurrent_net": "Líquido Recorrente Efetivo",
    "salario_hora": "Salário-Hora Efetivo",
    "overtime_vulnerability": "Vulnerabilidade de Horas Extras",
    "effective_tax_rate": "Alíquota Efetiva de Retenção",
    "projecao_anual": "Projeção de Entrada Anual",
    "inconsistencias": "Inconsistências Identificadas",
}

# Mapa de vazamentos de termos técnicos que o markdown/IA pode reproduzir.
# Aplicado como saneamento final em get_card_explanation e após o refine da IA.
_EXPLAIN_LEAK_MAP = dict(_CARD_TITLES)
_EXPLAIN_LEAK_MAP["net_value"] = "valor líquido"


def humanize_explanation(text) -> str:
    """Substitui termos técnicos (snake_case) por rótulos human-readable pt-BR.

    Remove qualquer resquício de identificadores com underline (ex.: a IA pode
    reproduzir `effective_tax_rate`, `net_value` etc.). O mapa cobre as chaves
    dos cards + `net_value`; como último recurso, um token restante com
    underline é "achatado" para espaços (nunca expõe snake_case ao usuário).
    """
    if not text:
        return ""
    out = str(text)
    for term, label in _EXPLAIN_LEAK_MAP.items():
        out = re.sub(
            r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
            label,
            out,
        )
    # Rede de segurança: qualquer snake_case remanescente vira texto espaçado.
    out = re.sub(
        r"\b([A-Za-zÀ-ÿ0-9]+(?:_[A-Za-zÀ-ÿ0-9]+)+)\b",
        lambda m: m.group(1).replace("_", " "),
        out,
    )
    return out


def _explain_advanced_card(
    card_id: str, user_id: int, mes_referencia=None, company_name=None
) -> Optional[str]:
    """Explicações dos cards alimentados por /analytics/advanced."""
    data = get_advanced_analytics(
        user_id, mes_referencia=mes_referencia, company_name=company_name
    )
    meta = data.get("meta") or {}
    overtime = data.get("overtime") or {}
    eff = data.get("effective_hourly") or {}
    taxes = data.get("tax_rates") or {}
    scope = (mes_referencia or "todo o período")

    if card_id == "recurrent_net":
        avg = meta.get("recurrent_net_pay") or 0.0
        months = meta.get("recurrent_net_months") or 0
        theoretical = meta.get("theoretical_recurrent_net") or 0.0
        return "\n".join(
            [
                f"- **O que representa:** Líquido recorrente médio de {months} mês(es) "
                f"completo(s) em {scope} — {_brl(avg)}/mês.",
                f"- **Insight do período:** Take-home recorrente {_brl(avg)}. "
                f"Referência teórica do perfil: {_brl(theoretical)}.",
                "- **Como é calculado:** soma do valor líquido (take-home) dos holerites "
                "dos meses completos ÷ nº de meses completos (regra de mês único respeitada).",
            ]
        )

    if card_id == "salario_hora":
        monthly = eff.get("monthly") or {}
        rate = monthly.get("rate") or 0.0
        net = monthly.get("net") or 0.0
        hours = monthly.get("hours") or 0.0
        return "\n".join(
            [
                f"- **O que representa:** Salário-hora efetivo em {scope} "
                f"({_brl(net)} / {hours:g} h = {_brl(rate)}/h).",
                f"- **Insight do período:** Cada hora útil rendeu {_brl(rate)} "
                f"considerando líquido recorrente + H.E./DSR.",
                "- **Como é calculado:** (líquido recorrente + net de H.E. ÷ meses) "
                "÷ (horas contratuais pró-rata + horas de H.E. ÷ meses).",
            ]
        )

    if card_id == "overtime_vulnerability":
        total = (overtime.get("total") or 0.0) + (overtime.get("dsr_overtime") or 0.0)
        ratio = overtime.get("ratio") or 0.0
        high = "ALTO" if ratio >= 30 else ("MODERADO" if ratio >= 15 else "baixo")
        return "\n".join(
            [
                f"- **O que representa:** Dependência de horas extras em {scope}: "
                f"{_brl(total)} ({ratio:g}% dos proventos).",
                f"- **Insight do período:** Nível de dependência {high}; quanto maior, "
                f"maior a variabilidade do líquido.",
                "- **Como é calculado:** (H.E. + DSR sobre H.E.) ÷ total de proventos "
                "× 100, dentro do filtro ativo.",
            ]
        )

    if card_id == "effective_tax_rate":
        # Hidrata o card "Alíquota Efetiva de Retenção" com a decomposição real
        # INSS + IRRF já calculada em get_advanced_analytics (tax_rates).
        inss = taxes.get("inss") or {}
        irrf = taxes.get("irrf") or {}
        inss_rate = inss.get("rate") or 0.0
        irrf_rate = irrf.get("rate") or 0.0
        inss_amount = inss.get("amount") or 0.0
        irrf_amount = irrf.get("amount") or 0.0
        gross = meta.get("total_earnings") or 0.0
        retention = inss_rate + irrf_rate
        return "\n".join(
            [
                f"- **O que representa:** Alíquota efetiva de retenção em {scope} — "
                f"{retention:g}% dos proventos brutos, decomposta em INSS "
                f"{inss_rate:g}% ({_brl(inss_amount)}) + IRRF {irrf_rate:g}% "
                f"({_brl(irrf_amount)}).",
                f"- **Insight do período:** Para cada R$ 100 brutos, {retention:g}% "
                f"ficam retidos em INSS e IRRF (sobre {_brl(gross)} de proventos).",
                "- **Como é calculado:** (INSS + IRRF) ÷ total de proventos × 100, "
                "usando as rubricas de retenção do período/filtro ativo.",
            ]
        )

    return None


def _explain_anomalies_card(
    user_id: int, mes_referencia=None, company_name=None
) -> str:
    """Explicação do card de inconsistências (auditoria escopável)."""
    flags = audit_paystub_anomalies(
        user_id, mes_referencia=mes_referencia, company_name=company_name
    )
    total = sum(float(f.get("amount") or 0.0) for f in flags)
    scope = (mes_referencia or company_name or "todo o histórico")
    top = None
    if flags:
        counts: dict = {}
        for f in flags:
            counts[f.get("category", "?")] = counts.get(f.get("category", "?"), 0) + 1
        top = max(counts.items(), key=lambda kv: kv[1])[0]
    return "\n".join(
        [
            f"- **O que representa:** Inconsistências/alertas em {scope}: "
            f"{len(flags)} ocorrência(s), impacto total {_brl(total)}.",
            f"- **Insight do período:**"
            + (f" Categoria mais frequente: {top}." if top else " Nenhum alerta relevante."),
            "- **Como é calculado:** auditoria mês a mês (médias móveis de 3 meses de "
            "benefícios, adicional noturno e faixa de IRRF) filtrada pelo card.",
        ]
    )


def _explain_annual_card(user_id: int) -> str:
    """Explicação do card global de Projeção de Entrada Anual."""
    from models.profile import load_profile as _load_profile

    db = get_db()
    profile = _load_profile(db, user_id)
    if profile is None:
        return "\n".join(
            [
                "- **O que representa:** Projeção de entrada anual (Acumulado Global).",
                "- **Insight do período:** Configure o perfil para ver a projeção.",
                "- **Como é calculado:** 12× líquido recorrente + 13º + férias + PPR.",
            ]
        )
    ppr_avg = 0.0
    rows = db.execute(
        "SELECT totals FROM holerites WHERE user_id=? AND tipo_documento='PPR'",
        [user_id],
    ).fetchall()
    if rows:
        nets = []
        for r in rows:
            try:
                nets.append(float((_load_totals(r)).get("net_value") or 0.0))
            except (ValueError, TypeError):
                continue
        ppr_avg = (sum(nets) / len(nets)) if nets else 0.0
    projection = get_annual_financial_projection(profile, {"ppr_avg": ppr_avg})
    total = projection.get("total_annual_take_home") or 0.0
    baseline = projection.get("baseline_annual") or 0.0
    return "\n".join(
        [
            "- **O que representa:** Projeção de entrada anual (Acumulado Global) — "
            f"{_brl(total)} ao ano.",
            f"- **Insight do período:** Baseline 12m {_brl(baseline)} mais 13º, férias "
            "(1/3) e PPR; métrica NÃO muda ao filtrar por mês/empresa.",
            "- **Como é calculado:** (líquido recorrente × 12) + 13º + férias(1/3) + PPR.",
        ]
    )


def get_card_explanation(
    db,
    user_id: int,
    card_id: str,
    mes_referencia=None,
    company_name=None,
) -> dict:
    """
    Gera explicação concisa (3 bullets) de um card do dashboard, escopada por
    mês/empresa quando o card é dinâmico. Cards anuais permanecem globais.
    """
    card_id = (card_id or "").strip()
    title = _CARD_TITLES.get(card_id)
    if title is None:
        # NUNCA ecoa a chave técnica (snake_case) em mensagens de erro.
        raise ValueError("Card não suportado para explicação.")

    md = _explain_advanced_card(
        card_id, user_id, mes_referencia=mes_referencia, company_name=company_name
    )
    if md is None and card_id == "inconsistencias":
        md = _explain_anomalies_card(
            user_id, mes_referencia=mes_referencia, company_name=company_name
        )
    if md is None and card_id == "projecao_anual":
        md = _explain_annual_card(user_id)

    if md is None:
        raise ValueError("Card não suportado para explicação.")

    # Saneamento final: garante que nenhuma chave técnica (snake_case) escape.
    return {"card_id": card_id, "title": title, "markdown": humanize_explanation(md)}


