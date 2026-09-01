"""
deepseek_service.py
-------------------
Serviço de parsing universal de holerites usando a API da DeepSeek (LLM).

Dado o texto bruto extraído de um PDF, `enrich_paycheck_data()` envia o
texto ao modelo `deepseek-chat` e instrui o retorno de um JSON ESTRITO no
seguinte contrato:

    {
      "company_name":       string,
      "company_tax_id":      string | null,
      "reference_month":     "YYYY-MM",
      "doc_type":            "FOLHA_MENSAL" | "ADIANTAMENTO" | "FERIAS" | "PPR",
      "base_salary":         float,
      "total_earnings":      float,
      "total_deductions":    float,
      "net_value":           float,
      "line_items": [ { "code", "description", "type" ("PROVENTO"|"DESCONTO"), "amount" } ]
    }

Se `DEEPSEEK_API_KEY` não estiver configurada, ou se a chamada à API falhar,
usa o parser local de fallback (`pdf_parser.parse_fallback`), mantendo o
mesmo contrato — a aplicação nunca quebra por causa do LLM (best-effort).
"""
import json
import logging
from typing import Optional

import requests

from .pdf_parser import (
    VALID_DOC_TYPES,
    determine_paycheck_type,
    parse_fallback,
    resolve_reference_month,
)

logger = logging.getLogger(__name__)

# Prompt de sistema: exige resposta SOMENTE em JSON no contrato exato.
_SYSTEM_PROMPT = (
    "You are an expert Brazilian payroll (holerite/contracheque) parser. "
    "Extract the data from the payroll text and answer ONLY with valid JSON "
    "(no markdown, no extra commentary) matching EXACTLY this schema:\n"
    "{\n"
    '  "company_name": string,\n'
    '  "company_tax_id": string or null,\n'
    '  "reference_month": "YYYY-MM",\n'
    '  "doc_type": "FOLHA_MENSAL" or "ADIANTAMENTO" or "FERIAS" or "PPR",\n'
    '  "base_salary": number,\n'
    '  "total_earnings": number,\n'
    '  "total_deductions": number,\n'
    '  "net_value": number,\n'
    '  "line_items": [ { "code": string or null, "description": string, '
    '"type": "PROVENTO" or "DESCONTO", "amount": number } ]\n'
    "}\n"
    "Rules:\n"
    "- Use null for missing fields; do NOT invent data.\n"
    '- reference_month must be in "YYYY-MM" format.\n'
    "- total_earnings is the sum of PROVENTO line items; "
    "total_deductions is the sum of DESCONTO line items.\n"
    "- net_value = total_earnings - total_deductions.\n"
    "Classify the 'type' of each line item using standard Brazilian payroll "
    "deduction rules:\n"
    '- Use "DESCONTO" (deduction) when the rubric is located under the '
    "'Descontos' column OR starts with a standard deduction code/term such as: "
    "'/314' (INSS), '/401' (IRRF), '4518' (Taxa Negocial), '4600' (Odonto), "
    "'4601' (Seguro de Vida), '4613' (Fretado), '4621' (Refeitório), "
    "'/B02' (Adiantamento Quinzenal), 'MCTO', 'M389' (Provisão/Ajustes de "
    "Desconto), or any other explicit deduction item (e.g. Vale Transporte, "
    "Pensão Alimentícia, Empréstimo, Faltas).\n"
    '- Use "PROVENTO" (earning) for: Base Salary (code "1000"), Overtime '
    '("1503", "1507"), DSR/Repouso ("1697"), Bonuses/Gratificações ("1303", '
    '"1357"), Night Shift / Adicional Noturno, and PPR/PLR.\n'
    "- Respond with ONLY the JSON object, no surrounding text.\n"
)


def _to_float(value) -> float:
    """Converte um valor arbitrário em float, com fallback seguro."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(payload: dict) -> dict:
    """
    Normaliza a resposta da DeepSeek para o contrato exato, corrigindo
    tipos, doc_type e o tipo das rubricas (PROVENTO/DESCONTO).
    """
    # Rubricas itemizadas.
    line_items = []
    for item in (payload.get("line_items") or []):
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type") or "").upper()
        if "DESCONTO" in raw_type:
            itype = "DESCONTO"
        elif "PROVENTO" in raw_type or "PROVENTOS" in raw_type:
            itype = "PROVENTO"
        else:
            itype = "PROVENTO"
        line_items.append(
            {
                "code": item.get("code"),
                "description": str(item.get("description") or "").strip(),
                "type": itype,
                "amount": _to_float(item.get("amount")),
            }
        )

    # Tipo de documento (valida contra o conjunto permitido).
    doc_type = str(payload.get("doc_type") or "FOLHA_MENSAL").upper()
    if doc_type not in VALID_DOC_TYPES:
        doc_type = "FOLHA_MENSAL"

    return {
        "company_name": str(payload.get("company_name") or "").strip() or None,
        "company_tax_id": payload.get("company_tax_id") or None,
        "reference_month": payload.get("reference_month") or None,
        "doc_type": doc_type,
        "base_salary": _to_float(payload.get("base_salary")),
        "total_earnings": _to_float(payload.get("total_earnings")),
        "total_deductions": _to_float(payload.get("total_deductions")),
        "net_value": _to_float(payload.get("net_value")),
        "line_items": line_items,
    }


# ---------------------------------------------------------------------
# Safeguard server-side de classificação de rubricas
# ---------------------------------------------------------------------
# Regras de segurança independentes do LLM/parser: se a descrição ou o
# código da rubrica indicarem claramente uma dedução, o tipo é FORÇADO
# para "DESCONTO", garantindo consistência no modal de detalhamento.
_DEDUCTION_KEYWORDS = (
    "INSS",
    "IRRF",
    "TRIBUTO",
    "DESCONTO",
    "DESC.",
    "FRETADO",
    "REFEITÓRIO",
    "REFEITORIO",
    "SEGURO",
    "TAXA",
    "ADIANTAMENTO",
    "ODONTO",
    "PENSÃO",
    "EMPRE",
    "FALTAS",
)

# Prefixos de código que indicam dedução (inclui os citados no prompt).
_DEDUCTION_CODE_PREFIXES = (
    "/314",
    "/401",
    "/B02",
    "4518",
    "4600",
    "4601",
    "4613",
    "4621",
    "MCTO",
    "M389",
)


def _apply_desconto_safeguard(data: dict) -> dict:
    """
    Reclassifica rubricas claramente dedutíveis para "DESCONTO".

    Percorre `data["line_items"]` e, sempre que o código ou a descrição de um
    item contiver uma palavra/prefixo de dedução típica de folha brasileira,
    força `type` para "DESCONTO" (red badge no modal de detalhamento).

    Args:
        data (dict): payload no contrato estrito (com `line_items`).

    Returns:
        dict: o mesmo payload com os tipos de rubrica corrigidos.
    """
    for item in data.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().upper()
        desc = str(item.get("description") or "").upper()
        text = f"{code} {desc}"

        is_deduction = any(kw in desc for kw in _DEDUCTION_KEYWORDS) or any(
            prefix in text for prefix in _DEDUCTION_CODE_PREFIXES
        )
        if is_deduction:
            item["type"] = "DESCONTO"
    return data


def _recalculate_totals(data: dict) -> dict:
    """
    Recalcula os totais do holerite a partir das rubricas classificadas.

    Depois que a safeguard (`_apply_desconto_safeguard`) corrige os tipos dos
    `line_items`, eles passam a ser a fonte de verdade da classificação.
    Esta função re-soma os valores por categoria e sobrescreve os totais
    (total_earnings, total_deductions, net_value) para que os KPIs do
    dashboard fiquem consistentes com o modal de detalhamento.

    Regras:
        * sum_proventos > 0 -> total_earnings = sum_proventos
        * sum_descontos > 0 -> total_deductions = sum_descontos
        * net_value = total_earnings - total_deductions

    Args:
        data (dict): payload no contrato estrito (com `line_items`).

    Returns:
        dict: o mesmo payload com os totais recalculados.
    """
    sum_proventos = 0.0
    sum_descontos = 0.0
    for item in data.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        # Magnitude: descontos são débitos (o sinal '-' no PDF é indicador de
        # coluna, não de crédito). Ex.: MCPA '125,92-' conta 125,92 como desconto.
        amount = abs(_to_float(item.get("amount")))
        item_type = str(item.get("type") or "").upper()
        if item_type == "PROVENTO":
            sum_proventos += amount
        elif item_type == "DESCONTO":
            sum_descontos += amount

    total_earnings = _to_float(data.get("total_earnings"))
    total_deductions = _to_float(data.get("total_deductions"))

    if sum_proventos > 0:
        total_earnings = sum_proventos
    if sum_descontos > 0:
        total_deductions = sum_descontos

    data["total_earnings"] = total_earnings
    data["total_deductions"] = total_deductions
    data["net_value"] = total_earnings - total_deductions

    logger.info(
        "Recalculando totais a partir das rubricas classificadas: "
        "proventos=%.2f descontos=%.2f liquido=%.2f",
        total_earnings,
        total_deductions,
        data["net_value"],
    )
    return data


def _load_rubrica_catalog() -> dict:
    """Carrega {código normalizado: tipo} do catálogo oficial do banco."""
    try:
        from database.connection import get_db
        from .db_service import get_catalog_map

        return get_catalog_map(get_db())
    except Exception:
        logger.warning(
            "Catálogo de rubricas indisponível; pulando enforcement.",
            exc_info=True,
        )
        return {}


# Códigos que DEVEM sempre ser DESCONTO / PROVENTO (garantia determinística,
# independente de o catálogo do banco estar populado).
_MANDATORY_DESCONTO = {
    "/314", "/401", "/B02", "4601", "4613", "4621", "MCT0", "MCPA", "4600",
}
_MANDATORY_PROVENTO = {
    "/B01", "1000", "1352", "1503", "1507", "1523", "1533", "1600", "1697",
    "1703", "1707", "1723", "1733", "1800", "MCPO",
}


def _enforce_official_catalog(data: dict, catalog: Optional[dict] = None) -> dict:
    """
    Força o tipo de cada rubrica conforme o catálogo oficial (fonte de verdade).

    Primeiro aplica os códigos obrigatórios (hard-coded) e, em seguida, o
    catálogo do banco. O `type` é sobrescrito de forma determinística em TODOS
    os line_items, independentemente do que o LLM, o parser ou a posição de
    coluna tenham inferido.

    Args:
        data (dict): payload no contrato estrito (com `line_items`).
        catalog (dict, opcional): mapa {código: tipo}; se omitido, carrega
            do banco.

    Returns:
        dict: o mesmo payload com os tipos forçados.
    """
    if catalog is None:
        catalog = _load_rubrica_catalog()
    if not catalog:
        return data
    for item in data.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get("code") or "").strip().upper()
        # 1) Garantia determinística (hard-coded).
        if raw_code in _MANDATORY_DESCONTO:
            item["type"] = "DESCONTO"
            continue
        if raw_code in _MANDATORY_PROVENTO:
            item["type"] = "PROVENTO"
            continue
        # 2) Catálogo do banco (casamento exato; fallback normalizado).
        tipo = catalog.get(raw_code)
        if tipo is None:
            tipo = catalog.get(raw_code.lstrip("/"))
        if tipo is not None:
            item["type"] = tipo
    return data


def _merge_position_types(data: dict, position_items) -> dict:
    """
    Mescla os tipos derivados da posição de coluna (pdfplumber) nas rubricas.

    Para itens já presentes em `data["line_items"]`, o `type` é substituído
    pelo da extração posicional (mais confiável que a inferência por texto).
    Itens que a extração posicional detectou mas o LLM/regex perdeu (ex.:
    '/401', 'MCPA') são adicionados.

    Args:
        data (dict): payload no contrato estrito (com `line_items`).
        position_items (list): saída de `pdf_parser.extract_line_items_pdf`.

    Returns:
        dict: o mesmo payload com tipos mesclados pela posição de coluna.
    """
    if not position_items:
        return data
    pos_by_code = {}
    for it in position_items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip().upper().lstrip("/")
        if code:
            pos_by_code[code] = it

    existing = data.get("line_items") or []
    seen = set()
    for item in existing:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().upper().lstrip("/")
        if code in pos_by_code:
            item["type"] = pos_by_code[code]["type"]
            seen.add(code)

    for code, it in pos_by_code.items():
        if code not in seen:
            existing.append(
                {
                    "code": it.get("code"),
                    "description": it.get("description"),
                    "type": it.get("type"),
                    "amount": _to_float(it.get("amount")),
                }
            )
    data["line_items"] = existing
    return data


class DeepSeekClient:
    """Cliente leve para a API /chat/completions da DeepSeek."""

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        """A integração só está ativa se houver chave configurada."""
        return bool(self.api_key)

    def chat_json(self, raw_text: str, model: str = "deepseek-chat") -> Optional[dict]:
        """
        Envia o texto ao modelo e retorna o JSON extraído (dict) ou None.

        Em caso de erro de rede/API ou JSON inválido, retorna None para que
        o chamador use o fallback local.
        """
        if not self.enabled:
            logger.info("DeepSeek não configurado; pulando chamada LLM.")
            return None

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

        try:
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("Falha ao consultar DeepSeek: %s", exc)
            return None

    def enrich_paycheck_data(self, raw_text: str, filename: Optional[str] = None) -> dict:
        """Extrai dados no contrato estrito, com fallback local garantido."""
        if not self.enabled or not raw_text:
            return parse_fallback(raw_text, filename=filename)

        payload = self.chat_json(raw_text)
        if not isinstance(payload, dict):
            logger.warning("Resposta DeepSeek inválida; usando fallback local.")
            return parse_fallback(raw_text, filename=filename)

        try:
            return _normalize(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Falha ao normalizar resposta DeepSeek: %s", exc)
            return parse_fallback(raw_text, filename=filename)


def enrich_paycheck_data(
    raw_text: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    line_items_override: Optional[list] = None,
    filename: Optional[str] = None,
) -> dict:
    """
    Função principal: interpreta um holerite e devolve dados no contrato.

    Chama a DeepSeek se houver chave; caso contrário, ou em falha, cai no
    parser local (`parse_fallback`) mantendo o mesmo formato de saída.

    Aplica, em ordem:
        1. Safeguard por palavra/prefixo (`_apply_desconto_safeguard`).
        2. Tipos derivados da posição de coluna do PDF (`_merge_position_types`).
        3. Catálogo oficial (`_enforce_official_catalog`) — fonte de verdade.
        4. Recálculo dos totais (`_recalculate_totals`).
        5. Tipo do documento e mês de competência determinísticos
           (`determine_paycheck_type` / `resolve_reference_month`), usando o
           nome do arquivo (ex.: 'agosto' -> 2026-08).

    Args:
        raw_text (str): texto bruto extraído do PDF.
        api_key (str, opcional): chave da API DeepSeek.
        base_url (str, opcional): URL base da API.
        line_items_override (list, opcional): itens da extração posicional.
        filename (str, opcional): nome do arquivo original (define tipo e mês).

    Returns:
        dict no contrato estrito (ver docstring do módulo).
    """
    client = DeepSeekClient(
        api_key=api_key or "",
        base_url=base_url or "https://api.deepseek.com",
    )
    data = _apply_desconto_safeguard(
        client.enrich_paycheck_data(raw_text, filename=filename)
    )
    data = _merge_position_types(data, line_items_override)
    data = _enforce_official_catalog(data)
    data = _recalculate_totals(data)

    # Tipo do documento e mês de competência finais (heurística determinística).
    data["doc_type"] = determine_paycheck_type(
        data.get("line_items") or [],
        data.get("total_earnings") or 0.0,
        filename or "",
    )
    data["reference_month"] = resolve_reference_month(
        data.get("reference_month"), raw_text, filename or ""
    )
    return data

