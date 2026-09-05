"""
services/ai_service.py
----------------------
Serviço de IA (DeepSeek) para o dashboard financeiro, com foco em segurança:

  * Integração com a API da DeepSeek (``deepseek-chat``) com parâmetros
    rígidos: ``max_tokens=250`` e ``temperature=0.3``.
  * Chave carregada estritamente via ``os.getenv("DEEPSEEK_API_KEY")``.
  * Proteção contra Prompt Injection e validação de entrada:
      - Tamanho máximo de 200 caracteres por consulta (HTTP 400 se exceder).
      - Rejeição de padrões de jailbreak / sobrescrita de sistema.
  * Controle de consumo (rate limiting) do modelo Freemium usando a tabela
    ``ai_usage_logs``:
      - Plano Gratuito -> máx. 5 consultas por janela de 24h.
      - Plano Pró      -> máx. 15 consultas por janela de 1h.
  * Best-effort: se a chave não estiver configurada, ou a chamada falhar,
    devolve uma resposta local determinística — a aplicação nunca quebra
    por causa do LLM.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from models.user import PLAN_FREE, PLAN_PRO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Constantes de segurança / governança de tokens
# ---------------------------------------------------------------------
MAX_QUERY_LENGTH = 200          # limite de caracteres por consulta
AI_MAX_TOKENS = 250             # teto rígido de geração (tokens)
AI_TEMPERATURE = 0.3            # geração determinística/segura

# Limites do modelo Freemium: {plano: (max_consultas, janela_segundos)}.
AI_PLAN_LIMITS: Dict[str, tuple] = {
    PLAN_FREE: (5, 24 * 60 * 60),   # 5 consultas / 24h
    PLAN_PRO: (15, 60 * 60),        # 15 consultas / 1h
}

# Padrões de prompt injection / jailbreak (minúsculas) a serem rejeitados.
_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous",
    "ignore the system",
    "system prompt",
    "you are now",
    "you are a",                       # tentativa de re-papear o modelo
    "act as",
    "jailbreak",
    "jailbreak mode",
    "developer mode",
    "dan mode",
    "do anything now",
    "disregard",
    "override your instructions",
    "forget your instructions",
    "forget everything",
    "reveal your instructions",
    "reveal your system prompt",
    "print your instructions",
    "output your system prompt",
    "repeat the prompt",
    "ignore the rules above",
    "ignore instructions",
]


class AIValidationError(Exception):
    """Erro de validação/limite retornado ao cliente como JSON."""

    status_code = 400

    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error = error
        self.message = message

    def payload(self) -> dict:
        return {"error": self.error, "message": self.message}


class QueryTooLongError(AIValidationError):
    """Consulta excede o tamanho máximo permitido."""

    status_code = 400

    def __init__(self, length: int):
        super().__init__(
            "QUERY_TOO_LONG",
            f"Sua pergunta tem {length} caracteres (máximo de {MAX_QUERY_LENGTH}). "
            "Encurte-a e tente novamente.",
        )


class PromptInjectionError(AIValidationError):
    """Entrada detectada como tentativa de manipulação do modelo."""

    status_code = 400

    def __init__(self):
        super().__init__(
            "PROMPT_INJECTION",
            "Entrada não permitida. Sua pergunta contém instruções de sistema "
            "ou tentativas de manipulação.",
        )


class RateLimitError(AIValidationError):
    """Usuário atingiu o limite de consultas de IA."""

    status_code = 429

    def __init__(self):
        super().__init__(
            "RATE_LIMIT_EXCEEDED",
            "Limite de consultas atingido. Faça upgrade para o Plano Pró "
            "para ter mais acessos.",
        )


def _resolve_api_key(api_key: Optional[str]) -> str:
    """
    Resolve a chave da API.

    A chave é carregada estritamente via ``os.getenv("DEEPSEEK_API_KEY")``.
    Quando uma chave explícita é informada (ex.: vinda da config do app),
    ela tem precedência — e um valor vazio/falsy desabilita a IA (fallback
    local determinístico), garantindo que os testes fiquem offline.
    """
    if api_key is not None:
        return api_key
    return os.getenv("DEEPSEEK_API_KEY", "")


def sanitize_query(query: str) -> str:
    """
    Valida e saneia a pergunta do usuário contra abusos.

    Regras:
      1. A entrada não pode ser vazia.
      2. Não pode exceder ``MAX_QUERY_LENGTH`` (HTTP 400 se exceder).
      3. Não pode conter padrões de jailbreak/sobrescrita de sistema.

    Returns:
        str: a pergunta normalizada (sem espaços nas bordas).
    """
    text = str(query or "").strip()
    if not text:
        raise AIValidationError("QUERY_EMPTY", "Digite uma pergunta.")
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryTooLongError(len(text))
    lowered = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lowered:
            logger.warning("Prompt injection bloqueada: %r", pattern)
            raise PromptInjectionError()
    # Remove quebras de linha excessivas que poderiam injetar novas mensagens.
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _chat(messages: List[dict], api_key: str, base_url: str) -> Optional[str]:
    """
    Chama o endpoint de chat da DeepSeek e retorna o texto da resposta.

    Returns:
        str: conteúdo da resposta, ou None em caso de falha.
    """
    if not api_key:
        return None
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": AI_MAX_TOKENS,     # teto rígido
        "temperature": AI_TEMPERATURE,   # determinístico/seguro
    }
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return str(content).strip()
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning("Falha ao consultar DeepSeek: %s", exc)
        return None


# ---------------------------------------------------------------------
# Contextualização e geração de respostas
# ---------------------------------------------------------------------
# Âncoras temporais do sistema (competência corrente e última folha fechada).
_SYSTEM_ANCHORS = {
    "current_period": "2026-09",
    "latest_closed_payroll": "2026-08",
}

# Prompt mestre ÚNICO para TODAS as interações com o LLM. Consolida o grounding
# temporal, as regras de categoria/vocabulário, a metodologia de projeção do ano
# corrente e a obrigação de nunca recair em "sem dados" quando há números reais.
_MASTER_SYSTEM_PROMPT = (
    "Você é um assistente financeiro especialista em folha de pagamento brasileira "
    "(holerites). Responda em português, de forma curta e objetiva, baseando-se "
    "APENAS nos dados fornecidos pelo usuário. "
    "Contexto temporal do sistema: "
    "O ano e mês corrente do sistema é Setembro de 2026. A folha mensal fechada "
    "mais recente cadastrada é Agosto de 2026 (2026-08). O valor de crédito em "
    "04/09/2026 refere-se ao pagamento da folha de Agosto. "
    "Regras de categoria e vocabulário: "
    "Considere EXCLUSIVAMENTE registros da categoria FOLHA_MENSAL ao responder "
    "sobre salário mensal, maior/menor salário do ano e médias de rendimento. "
    "Ignorar PPR, Adiantamento e 13º nessas análises. NUNCA utilize termos técnicos "
    "em inglês ou com underline (ex: recurrent_net, effective_tax_rate, card_id). "
    "Traduza e humanize sempre para o português nativo. "
    "Metodologia de projeção financeira: "
    "Ao calcular a projeção de salário/rendimento para fechar o ano corrente: "
    "1. Identifique o acumulado real já pago nas folhas mensais fechadas "
    "(Jan a Ago = 8 meses). "
    "2. Calcule a média informada ou solicitada (ex: últimos 3 meses: Junho, "
    "Julho e Agosto). "
    "3. Projete exatamente os 4 meses restantes do ano (Setembro, Outubro, "
    "Novembro e Dezembro) multiplicando a média por 4. "
    "4. Especifique de forma clara e transparente a fórmula aplicada: "
    "Acumulado + (Média × 4 meses restantes) + 13º Salário (se aplicável). "
    "Qualidade da resposta: "
    "Se houver dados numéricos no contexto fornecido, NUNCA responda que 'não há "
    "dados suficientes'. Utilize os números disponíveis no JSON para realizar os "
    "cálculos e forneça respostas diretas e fundamentadas. "
    "Use a tabela mês a mês para apontar o valor mínimo/máximo e eventuais "
    "tendências entre competências."
)


# ---------------------------------------------------------------------
# Privacidade (LGPD) — minimização & anonimização de dados
# ---------------------------------------------------------------------
# Campos de identificação pessoal que NUNCA podem chegar ao prompt do LLM.
# Cobre os nomes do enunciado + chaves reais do schema que carregam PII.
_PII_FIELD_KEYS = frozenset({
    "nome",
    "name",
    "employee_name",
    "employee",
    "funcionario",
    "cpf",
    "cnpj",
    "matricula",
    "email",
    "cargo",
    "job_title",
    "empresa",
    "company",
    "company_name",
    "company_tax_id",
    "worker_name",
    "subarea",
    "estabelecimento",
    "admission_date",
    "raw_text",
})


def anonymize_payload(payload):
    """Remove recursivamente campos de PII antes de compor o contexto da IA.

    Garante que o LLM receba APENAS métricas numéricas, rubricas, taxas e
    competências (ex.: ``{"competencia": "2026-08", "proventos": 15890.03}``)
    — nunca dados pessoais (nome, CPF, matrícula, e-mail, cargo, empresa...).
    """
    if isinstance(payload, dict):
        return {
            key: anonymize_payload(value)
            for key, value in payload.items()
            if str(key).strip().lower() not in _PII_FIELD_KEYS
        }
    if isinstance(payload, list):
        return [anonymize_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(anonymize_payload(item) for item in payload)
    return payload


def _format_ai_context(context: Optional[dict]) -> str:
    """
    Formata o contexto dos holerites como uma tabela mês a mês + agregados.

    Aceita o contexto rico (`build_ai_paystub_context`) ou um dict achatado
    (fallback com totais simples), sempre retornando texto seguro.
    """
    if not isinstance(context, dict):
        return "- (sem dados)"

    # LGPD: remove qualquer PII residual antes de montar o texto do prompt.
    context = anonymize_payload(context)

    monthly = context.get("monthly") or []
    anchors = _SYSTEM_ANCHORS
    parts = [
        "Referência temporal do sistema: "
        f"current_period: {anchors['current_period']}; "
        f"latest_closed_payroll: {anchors['latest_closed_payroll']}."
    ]

    if monthly:
        header = "| Competência | Categoria | Bruto | Líquido | INSS | IRRF |"
        lines = [header]
        for m in monthly:
            categoria = str(m.get("categoria") or "FOLHA_MENSAL")
            comp = str(m.get("competencia") or m.get("mes_referencia") or "")
            lines.append(
                f"| {comp} | {categoria} | R$ {m.get('gross', 0):,.2f} | "
                f"R$ {m.get('net', 0):,.2f} | R$ {m.get('inss', 0):,.2f} | "
                f"R$ {m.get('irrf', 0):,.2f} |"
            )
        parts.append("Holerites mês a mês (apenas FOLHA_MENSAL):\n" + "\n".join(lines))

        with_rubrics = [m for m in monthly if m.get("rubrics")]
        if with_rubrics:
            rlines = []
            for m in with_rubrics:
                names = ", ".join(
                    f"{k}: R$ {v:,.2f}" for k, v in list(m["rubrics"].items())[:5]
                )
                rlines.append(f"- {m['competencia']}: {names}")
            parts.append("Rubricas/deduções por mês:\n" + "\n".join(rlines))

    aggr = context.get("aggregates") or {}
    if aggr:
        parts.append(
            "Resumo:\n"
            f"- Total (YTD) bruto: R$ {aggr.get('ytd_gross', 0):,.2f}; "
            f"líquido: R$ {aggr.get('ytd_net', 0):,.2f}\n"
            f"- Média bruta: R$ {aggr.get('avg_gross', 0):,.2f}; "
            f"menor: R$ {aggr.get('min_gross', 0):,.2f} ({aggr.get('min_month', '')}); "
            f"maior: R$ {aggr.get('max_gross', 0):,.2f} ({aggr.get('max_month', '')})\n"
            f"- YTD INSS: R$ {aggr.get('ytd_inss', 0):,.2f}; "
            f"YTD IRRF: R$ {aggr.get('ytd_irrf', 0):,.2f}"
        )

    if not (monthly or aggr):
        for key in ("count", "total_earnings", "total_deductions", "net_value", "tax_rate"):
            if key in context:
                parts.append(f"- {key}: {context[key]}")

    return "\n\n".join(parts) if parts else "- (sem dados)"


def ask_ai(
    question: str,
    context: Optional[dict] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """
    Responde a uma pergunta do usuário contextualizada com o breakdown mensal
    dos seus holerites (dados reais do dashboard).
    """
    question = sanitize_query(question)
    context = context or {}
    base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    api_key = _resolve_api_key(api_key)

    messages = [
        {"role": "system", "content": _MASTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Dados do meu dashboard financeiro:\n"
                f"{_format_ai_context(context)}\n\n"
                f"Pergunta: {question}"
            ),
        },
    ]
    answer = _chat(messages, api_key, base_url)
    if answer is None:
        answer = (
            "Não foi possível consultar o assistente neste momento. "
            "Com base nos seus dados, revise os valores exibidos no painel "
            "e consulte a seção de Inconsistências para detalhes."
        )
    return answer


def explain_anomaly(
    anomaly: dict,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """
    Gera uma explicação curta (2 frases) para uma inconsistência detectada,
    incluindo uma ação recomendada.
    """
    title = str(anomaly.get("title") or "Anomalia detectada")
    description = str(anomaly.get("description") or "")
    month = str(anomaly.get("month") or "")
    amount = anomaly.get("amount")

    base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    api_key = _resolve_api_key(api_key)

    facts = (
        f"Anomalia: {title}. Detalhe: {description}."
        + (f" Competência: {month}." if month else "")
        + (f" Impacto monetário: R$ {amount}." if isinstance(amount, (int, float)) else "")
    )

    messages = [
        {"role": "system", "content": _MASTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Explique em EXATAMENTE 2 frases a seguinte inconsistência de "
                "folha de pagamento e sugira uma ação recomendada (sem listar "
                "passos longos).\n"
                f"{facts}"
            ),
        },
    ]
    answer = _chat(messages, api_key, base_url)
    if answer is None:
        answer = (
            f"Detectamos uma variação relevante em '{title}'"
            + (f" na competência {month}" if month else "")
            + ". Revise os descontos lançados no holerite e, se a divergência "
            "for confirmada, abra uma reclamação de folha junto ao RH."
        )
    return answer


def explain_card(
    title: str,
    markdown: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """
    Explica um card do dashboard em EXATAMENTE 3 bullets de markdown.

    Diferente de `ask_ai` (perguntas de usuário), este é um prompt interno de
    sistema: não passa pelo teto de ``MAX_QUERY_LENGTH`` e o `markdown`
    determinístico (com os valores numéricos reais) é enviado como contexto,
    para que o modelo NUNCA alegue falta de dados quando há números presentes.
    O `title` é o rótulo human-readable (nunca a chave técnica snake_case).
    """
    base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    api_key = _resolve_api_key(api_key)
    title = str(title or "indicador")
    context_md = str(markdown or "- (sem valores)").strip()

    instruction = (
        f"Analise os dados financeiros fornecidos no contexto e explique o "
        f"indicador '{title}' em exatamente 3 bullet points de markdown "
        "(O que representa, Insight do período, Como é calculado). "
        "NUNCA diga que não há dados suficientes se os valores numéricos "
        "estiverem no contexto. NUNCA use nomes de variáveis em inglês ou "
        "com underline (ex: recurrent_net, effective_tax_rate)."
    )

    messages = [
        {"role": "system", "content": _MASTER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Indicador analisado: {title}.\n\n"
                f"Valores calculados no dashboard (contexto):\n{context_md}\n\n"
                f"Instrução: {instruction}"
            ),
        },
    ]
    answer = _chat(messages, api_key, base_url)
    if answer is None:
        answer = (
            f"Com base nos dados exibidos para '{title}', revise os valores do "
            "indicador no painel e, havendo divergência, consulte a seção de "
            "Inconsistências para detalhes."
        )
    return answer


# ---------------------------------------------------------------------
# Governança de consumo (ai_usage_logs) & rate limiting
# ---------------------------------------------------------------------
def ensure_ai_usage_logs_table(db) -> None:
    """Garante a existência da tabela `ai_usage_logs` (idempotente)."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
            ON ai_usage_logs (user_id, created_at)
        """
    )


def ensure_ai_blocked_logs_table(db) -> None:
    """
    Garante a existência da tabela `ai_blocked_logs` (idempotente).

    Registra tentativas de prompt injection / entradas inválidas rejeitadas
    pelo `ai_service`, para a auditoria de governança de IA (FinOps).
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_blocked_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason     TEXT    NOT NULL,
            detail     TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_blocked_user_created
            ON ai_blocked_logs (user_id, created_at)
        """
    )


def log_blocked_attempt(db, user_id: int, reason: str, detail: Optional[str] = None) -> None:
    """Registra uma tentativa de prompt injection rejeitada (auditoria)."""
    ensure_ai_blocked_logs_table(db)
    db.execute(
        "INSERT INTO ai_blocked_logs (user_id, reason, detail) VALUES (?, ?, ?)",
        [user_id, reason, detail or ""],
    )
    db.commit()


def get_ai_limits(db) -> Dict[str, tuple]:
    """
    Retorna os limites de IA por plano, lendo os valores dinâmicos do modelo
    Freemium (com fallback para os padrões `AI_PLAN_LIMITS`).

    Returns:
        dict: {plano: (max_consultas, janela_segundos)}
    """
    from services.settings_service import get_freemium_limits

    limits = dict(AI_PLAN_LIMITS)
    settings = get_freemium_limits(db)
    limits[PLAN_FREE] = (settings["free_ai_queries_per_day"], 24 * 60 * 60)
    limits[PLAN_PRO] = (settings["pro_ai_queries_per_hour"], 60 * 60)
    return limits


def get_user_plan(db, user_id: int) -> str:
    """Retorna o plano do usuário ('free' | 'pro'), com default 'free'."""
    row = db.execute(
        "SELECT plan FROM users WHERE id = ?", [user_id]
    ).fetchone()
    plan = str((row["plan"] if row else "") or PLAN_FREE).lower()
    return plan if plan in (PLAN_FREE, PLAN_PRO) else PLAN_FREE


def log_ai_usage(db, user_id: int) -> None:
    """Registra uma chamada válida à IA para o usuário."""
    db.execute(
        "INSERT INTO ai_usage_logs (user_id) VALUES (?)", [user_id]
    )
    db.commit()


def count_queries_in_window(db, user_id: int, seconds: int) -> int:
    """Conta consultas de IA do usuário na janela (em segundos) informada."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM ai_usage_logs
        WHERE user_id = ? AND created_at >= ?
        """,
        [user_id, cutoff],
    ).fetchone()
    return int(row["c"]) if row else 0


def enforce_ai_rate_limit(db, user_id: int) -> dict:
    """
    Aplica o rate limit de IA conforme o plano do usuário (limites dinâmicos).

    Returns:
        dict: {"plan", "limit", "window", "used", "remaining"}

    Raises:
        RateLimitError: quando o limite da janela foi atingido (HTTP 429).
    """
    plan = get_user_plan(db, user_id)
    limit, window = get_ai_limits(db).get(plan, get_ai_limits(db)[PLAN_FREE])
    used = count_queries_in_window(db, user_id, window)
    if used >= limit:
        raise RateLimitError()
    return {
        "plan": plan,
        "limit": limit,
        "window": window,
        "used": used,
        "remaining": max(limit - used, 0),
    }


def get_ai_cooldown_seconds(db, user_id: int) -> int:
    """
    Segundos restantes até liberar uma nova consulta (janela do plano).

    Retorna 0 quando o usuário ainda tem crédito disponível. Quando atingiu o
    limite, conta a partir da consulta MAIS ANTIGA ainda dentro da janela
    (a primeira a expirar). Usado para exibir o tempo de espera no modal.
    """
    plan = get_user_plan(db, user_id)
    limit, window = get_ai_limits(db).get(plan, get_ai_limits(db)[PLAN_FREE])
    used = count_queries_in_window(db, user_id, window)
    if used < limit:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        """
        SELECT MIN(created_at) AS oldest
        FROM ai_usage_logs
        WHERE user_id = ? AND created_at >= ?
        """,
        [user_id, cutoff],
    ).fetchone()
    oldest_raw = (row["oldest"] if row else None)
    if not oldest_raw:
        return 0
    try:
        oldest = datetime.strptime(oldest_raw, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return 0
    return max(int((oldest + timedelta(seconds=window) - now).total_seconds()), 0)


def get_ai_usage_summary(db, user_id: int) -> dict:
    """Resumo de consumo de IA do usuário para o frontend (limites dinâmicos)."""
    plan = get_user_plan(db, user_id)
    limit, window = get_ai_limits(db).get(plan, get_ai_limits(db)[PLAN_FREE])
    used = count_queries_in_window(db, user_id, window)
    return {
        "plan": plan,
        "limit": limit,
        "window": window,
        "used": used,
        "remaining": max(limit - used, 0),
    }
