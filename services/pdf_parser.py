"""
pdf_parser.py
-------------
Serviço responsável por extrair texto e dados estruturados de PDFs de
holerites (contracheques).

Combina duas bibliotecas:
  * `pdfplumber` -> melhor para extração textual com layout.
  * `pypdf`      -> fallback robusto para documentos simples.

O serviço expõe funções puras (sem dependência do Flask) para facilitar
testes e reutilização. A extração de dados estruturados (empresa, totais,
rubricas) é feita por expressões regulares, podendo ser enriquecida pela
integração com a DeepSeek (ver `deepseek_service.py`).
"""
import hashlib
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import List, Optional

import pdfplumber
from pypdf import PdfReader

# ---------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------
@dataclass
class Rubrica:
    """Uma linha itemizada de um holerite."""
    codigo: Optional[str] = None
    descricao: str = ""
    tipo: str = "provento"          # 'provento' | 'desconto'
    valor: float = 0.0
    referencia: Optional[str] = None


@dataclass
class HoleriteData:
    """Dados estruturados extraídos de um PDF de holerite."""
    company_name: Optional[str] = None
    company_tax_id: Optional[str] = None
    funcionario: Optional[str] = None
    mes_referencia: Optional[str] = None
    tipo_documento: str = "holerite"
    totals: dict = field(default_factory=dict)
    bases: dict = field(default_factory=dict)
    rubricas: List[Rubrica] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _clean(value: str) -> str:
    """Remove espaços extras e normaliza espaçamento interno."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def compute_file_hash(content: bytes) -> str:
    """Gera SHA-256 do conteúdo do arquivo (para evitar duplicatas)."""
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------
def extract_text_from_bytes(content: bytes) -> str:
    """
    Extrai o texto de um PDF a partir de bytes.

    Tenta primeiro com `pdfplumber`; se falhar, usa `pypdf` como fallback.

    Args:
        content (bytes): conteúdo binário do arquivo PDF.

    Returns:
        str: texto extraído de todas as páginas.
    """
    # 1) Tentativa principal com pdfplumber (melhor layout).
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
    except Exception:
        pass  # cai no fallback

    # 2) Fallback com pypdf.
    reader = PdfReader(BytesIO(content))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(pages)


# ---------------------------------------------------------------------
# Extração de dados estruturados
# ---------------------------------------------------------------------
def _parse_money(value: str) -> float:
    """Converte '1.234,56' ou '1234.56' em float. Retorna 0.0 se inválido."""
    if not value:
        return 0.0
    cleaned = value.strip().replace("R$", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")  # pt-BR -> en
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# Padrões comuns em holerites brasileiros.
_PAT_COMPANY = re.compile(r"(empresa|raz[ãa]o social|cnpj)\s*[:.]?\s*(.+)", re.I)
_PAT_CNPJ = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b")
_PAT_FUNC = re.compile(r"(funcion[áa]rio|colaborador|empregado)\s*[:.]?\s*(.+)", re.I)
_PAT_MES = re.compile(
    r"(compet[êe]ncia|mes/ano|refer[êe]ncia)\s*[:.]?\s*(\d{2}/\d{4}|\d{1,2}/\d{4})", re.I
)


def parse_holerite_text(text: str) -> HoleriteData:
    """
    Faz a análise estruturada do texto bruto de um holerite.

    Extrai empresa, CNPJ, funcionário, mês de competência e uma primeira
    tentativa de totais/rubricas. A interpretação fina pode ser delegada à
    DeepSeek (função `enhance_with_llm` em `deepseek_service.py`).

    Args:
        text (str): texto bruto extraído do PDF.

    Returns:
        HoleriteData: dados estruturados.
    """
    data = HoleriteData(raw_text=text)
    lines = [line for line in text.splitlines() if line.strip()]

    # Empresa: primeira linha costuma conter o nome do empregador.
    if lines:
        first = _clean(lines[0])
        if first and len(first) < 120:
            data.company_name = first

    # CNPJ/CPF do empregador.
    cnpj = _PAT_CNPJ.search(text)
    if cnpj:
        data.company_tax_id = cnpj.group(0)

    # Funcionário.
    func = _PAT_FUNC.search(text)
    if func:
        data.funcionario = _clean(func.group(2))

    # Mês de competência.
    mes = _PAT_MES.search(text)
    if mes:
        data.mes_referencia = mes.group(2)

    return data


def parse_pdf(content: bytes) -> HoleriteData:
    """
    API principal: extrai texto do PDF e devolve os dados estruturados.

    Args:
        content (bytes): bytes do arquivo PDF.

    Returns:
        HoleriteData: dados do holerite com texto bruto incluído.
    """
    text = extract_text_from_bytes(content)
    data = parse_holerite_text(text)
    data.raw_text = text
    return data


# ---------------------------------------------------------------------
# Fallback no contrato estrito (usado sem DeepSeek ou em caso de falha)
# ---------------------------------------------------------------------
# Tipos de documento suportados pelo contrato de extração.
VALID_DOC_TYPES = {"FOLHA_MENSAL", "ADIANTAMENTO", "FERIAS", "PPR"}

# Padrão para linhas de rubrica: código opcional + descrição + tipo + valor.
_PAT_LINE_ITEM = re.compile(
    r"(?P<code>^\d{2,6}|\d{2,6})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<type>PROVENTO|DESCONTO)\s+"
    r"(?P<amount>[+-]?\d{1,3}(?:\.\d{3})*,\d{2})",
    re.IGNORECASE,
)


def _to_iso_month(value: str) -> Optional[str]:
    """Converte '06/2024' ou '2024-06' em 'YYYY-MM'."""
    if not value:
        return None
    value = value.strip()
    if "/" in value:
        parts = value.split("/")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            mm = parts[0].zfill(2)
            yyyy = parts[1]
            if len(yyyy) == 2:
                yyyy = "20" + yyyy
            return f"{yyyy}-{mm}"
    return value


# ---------------------------------------------------------------------
# Tipo de documento e mês de competência (heurística determinística)
# ---------------------------------------------------------------------
# Mapa de meses em português -> número.
_MESES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def _month_from_filename(filename: Optional[str]) -> Optional[int]:
    """Extrai o número do mês a partir do nome do arquivo (ex.: 'agosto' -> 8)."""
    fname = (filename or "").lower()
    for name, num in _MESES.items():
        if name in fname:
            return num
    return None


def _year_from_text(raw_text: Optional[str]) -> Optional[int]:
    """Extrai um ano de 4 dígitos (20xx) do texto bruto do PDF."""
    m = re.search(r"\b(20\d{2})\b", raw_text or "")
    return int(m.group(1)) if m else None


def _year_from_month_str(month: Optional[str]) -> Optional[int]:
    """Extrai o ano de 'YYYY-MM' ou 'MM/YYYY'."""
    if month:
        m = re.search(r"(20\d{2})", month)
        if m:
            return int(m.group(1))
    return None


def resolve_reference_month(
    parsed_month: Optional[str],
    raw_text: Optional[str],
    filename: Optional[str] = None,
) -> Optional[str]:
    """
    Determina o mês de competência ('YYYY-MM').

    Prioriza o MÊS do nome do arquivo (ex.: 'agosto' -> 08) e o ANO do texto
    do PDF (ex.: 'Julho 2026' -> 2026), garantindo que '08 adiantamento
    agosto.pdf' resulte em '2026-08' mesmo que a data de pagamento no PDF
    seja de julho.
    """
    year = _year_from_month_str(parsed_month) or _year_from_text(raw_text)
    month = _month_from_filename(filename)

    if month is None:
        # Fallback: mês já parseado em 'MM/YYYY' ou 'YYYY-MM'.
        if parsed_month and "/" in parsed_month:
            try:
                month = int(parsed_month.split("/")[0])
            except (ValueError, IndexError):
                month = None
        elif parsed_month and len(parsed_month) >= 7 and parsed_month[5:7].isdigit():
            try:
                month = int(parsed_month[5:7])
            except ValueError:
                month = None

    if year is None:
        import datetime
        year = datetime.date.today().year

    if month is None:
        return parsed_month or None
    return f"{year:04d}-{month:02d}"


def determine_paycheck_type(
    line_items: list,
    total_earnings: float,
    filename: Optional[str] = None,
) -> str:
    """
    Determina o tipo do documento por heurística estrita.

    Regras:
      * PPR          -> código '1334' presente OU nome do arquivo contém 'ppr'.
      * ADIANTAMENTO -> nome do arquivo contém 'adiantamento' OU
                        '/B01' presente E total de proventos <= R$ 2.500,00.
      * FERIAS       -> nome do arquivo contém 'feria'.
      * padrão       -> 'FOLHA_MENSAL'.
    """
    codes = {str(i.get("code") or "").strip().upper() for i in (line_items or [])}
    fname = (filename or "").lower()

    if "1334" in codes or "ppr" in fname:
        return "PPR"
    if "adiantamento" in fname:
        return "ADIANTAMENTO"
    if ("/B01" in codes or "B01" in codes) and float(total_earnings or 0.0) <= 2500.0:
        return "ADIANTAMENTO"
    if "feria" in fname:
        return "FERIAS"
    return "FOLHA_MENSAL"


def parse_fallback(raw_text: str, filename: Optional[str] = None) -> dict:
    """
    Parser local de fallback que retorna dados no MESMO contrato estrito
    esperado da DeepSeek. Garante que a aplicação funcione sem chave de API
    ou quando a chamada ao LLM falhar (best-effort).

    Returns:
        dict no formato:
        {
          "company_name", "company_tax_id", "reference_month", "doc_type",
          "base_salary", "total_earnings", "total_deductions", "net_value",
          "line_items": [{"code", "description", "type", "amount"}]
        }
    """
    local = parse_holerite_text(raw_text)

    line_items = []
    for line in raw_text.splitlines():
        match = _PAT_LINE_ITEM.search(line)
        if match:
            line_items.append(
                {
                    "code": match.group("code"),
                    "description": _clean(match.group("desc")),
                    "type": match.group("type").upper(),
                    "amount": _parse_money(match.group("amount")),
                }
            )

    return {
        "company_name": local.company_name,
        "company_tax_id": local.company_tax_id,
        "reference_month": resolve_reference_month(
            _to_iso_month(local.mes_referencia), raw_text, filename
        ),
        "doc_type": "FOLHA_MENSAL",
        "base_salary": 0.0,
        "total_earnings": 0.0,
        "total_deductions": 0.0,
        "net_value": 0.0,
        "line_items": line_items,
    }


# ---------------------------------------------------------------------
# Extrator posicional (pdfplumber com coordenadas x0/x1)
# ---------------------------------------------------------------------
# O texto plano perde o alinhamento das colunas (Proventos/Descontos),
# fazendo itens como '/401' e 'MCPA' serem mal classificados. Este extrator
# usa as coordenadas das palavras para detectar a coluna "Descontos" e
# classificar cada valor pela sua posição horizontal — determinístico.
_MONEY_RE = re.compile(r"^-?[0-9][0-9.,]*-?$")


def _parse_money_pt(value: str) -> float:
    """Converte '1.976,48', '125,92-' ou '-543,53' em float (com sinal)."""
    s = (value or "").strip()
    if not s:
        return 0.0
    negative = False
    if s.endswith("-"):
        negative = True
        s = s[:-1]
    elif s.startswith("-"):
        negative = True
        s = s[1:]
    s = s.replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        n = float(s)
    except ValueError:
        return 0.0
    return -n if negative else n


def _looks_like_money(value: str) -> bool:
    """True se o texto parece um valor monetário pt-BR (ex.: '1.234,56', '125,92-')."""
    if not value:
        return False
    if not _MONEY_RE.match(value.strip()):
        return False
    return any(ch.isdigit() for ch in value)


def _group_words_by_top(words, tol: float = 3.0) -> List[List[dict]]:
    """Agrupa palavras extraídas em linhas, pelo `top` (eixo Y)."""
    lines: List[List[dict]] = []
    for w in sorted(words, key=lambda x: (x["top"], x["x0"])):
        for line in lines:
            if abs(line[0]["top"] - w["top"]) <= tol:
                line.append(w)
                break
        else:
            lines.append([w])
    lines.sort(key=lambda line: line[0]["top"])
    return lines


def _find_table_columns(lines):
    """
    Localiza o cabeçalho da tabela de rubricas e as fronteiras de coluna.

    Returns:
        tuple (header_idx, desc_left, prov_left, desconto_left) ou
        (None, None, None, None) se não achar o cabeçalho.
    """
    for idx, line in enumerate(lines):
        low = [w["text"].lower() for w in line]
        if "descontos" in low and "proventos" in low:
            desc_left = None
            prov_left = None
            desconto_left = None
            for w in line:
                t = w["text"].lower()
                if t in ("descrição", "descricao"):
                    desc_left = w["x0"]
                elif t == "proventos":
                    prov_left = w["x0"]
                elif t == "descontos":
                    desconto_left = w["x0"]
            if prov_left is not None and desconto_left is not None:
                if desc_left is None:
                    desc_left = prov_left
                return idx, desc_left, prov_left, desconto_left
    return None, None, None, None


def _row_to_line_item(words, desc_left, prov_left, desconto_left) -> Optional[dict]:
    """Converte uma linha (palavras com coordenadas) em um line_item."""
    row = sorted(words, key=lambda w: w["x0"])
    if not row:
        return None
    joined = " ".join(w["text"] for w in row).upper().replace(" ", "")
    if "TOTAIS" in joined:
        return None

    code = row[0]["text"]
    desc_words = [w["text"] for w in row if w["x0"] >= desc_left and w["x0"] < prov_left]
    description = _clean(" ".join(desc_words))

    amounts = [w for w in row if w["x0"] >= prov_left and _looks_like_money(w["text"])]
    if not amounts:
        return None
    money = max(amounts, key=lambda w: w["x0"])
    amount = _parse_money_pt(money["text"])
    itype = "DESCONTO" if money["x0"] >= desconto_left else "PROVENTO"
    return {
        "code": code,
        "description": description,
        "type": itype,
        "amount": amount,
    }


def extract_line_items_pdf(
    content: bytes,
    desconto_threshold: Optional[float] = None,
) -> List[dict]:
    """
    Extrai as rubricas de um PDF usando as coordenadas de coluna.

    Detecta o cabeçalho com 'Proventos'/'Descontos' e usa o X do cabeçalho
    'Descontos' como limite: qualquer valor à direita (ou igual) desse X é
    forçado para `type = "DESCONTO"`.

    Args:
        content (bytes): bytes do PDF.
        desconto_threshold (float, opcional): override do limite X da coluna
            'Descontos' (útil quando o cabeçalho não é detectável).

    Returns:
        List[dict]: [{code, description, type ('PROVENTO'|'DESCONTO'), amount}]
    """
    line_items: List[dict] = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words:
                continue
            lines = _group_words_by_top(words)
            idx, desc_left, prov_left, desconto_left = _find_table_columns(lines)

            if desconto_left is None:
                if desconto_threshold is None:
                    continue
                prov_left = prov_left if prov_left is not None else desconto_threshold - 100.0
                desc_left = desc_left if desc_left is not None else prov_left
                desconto_left = desconto_threshold
            if idx is None:
                continue

            for line in lines[idx + 1:]:
                joined = " ".join(w["text"] for w in line).upper().replace(" ", "")
                if "TOTAIS" in joined:
                    break  # linha de totais: para de coletar itens
                item = _row_to_line_item(line, desc_left, prov_left, desconto_left)
                if item:
                    line_items.append(item)
    return line_items
