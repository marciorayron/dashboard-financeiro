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
    prev_limit = 0.0
    for limit, rate, accum in brackets:
        if gross <= limit:
            return accum + (gross - prev_limit) * rate
        prev_limit = limit
    # acima do teto: mantém a última faixa.
    _, rate, accum = brackets[-1]
    return accum + (gross - brackets[-1][0]) * rate


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
        },
        "overtime": {
            "total": round(overtime_total, 2),
            "dsr_overtime": round(dsr_overtime_total, 2),
            "ratio": round(overtime_ratio, 2),
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


def audit_paystub_anomalies(user_id: int) -> List[dict]:
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
        ORDER BY h.mes_referencia ASC
        """,
        [user_id],
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
                }
            )

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    flags.sort(key=lambda f: order.get(f["severity"], 9))
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

