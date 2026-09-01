"""
db_service.py
-------------
Serviço de manutenção e migrações do banco de dados SQLite.

A principal rotina (`fix_provento_classification`) corrige retroativamente
rubricas que foram gravadas incorretamente como 'provento' mas representam
deduções (INSS, IRRF, Taxa Negocial, Fretado, Adiantamento, etc.). Isso
cobre dados históricos — a safeguard em `deepseek_service` só vale para
novos uploads.

Também recalcula `total_earnings`, `total_deductions` e `net_value` dos
holerites que tiveram rubricas reclassificadas.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Palavras que, quando presentes na descrição, indicam uma dedução (DESCONTO).
DEDUCTION_KEYWORDS = (
    "INSS",
    "IRRF",
    "TAXA",
    "FRETADO",
    "ADTO",
    "ADIANTAMENTO",
    "REFEITÓRIO",
    "REFEITORIO",
    "SEGURO",
    "DESCONTO",
    "DESC.",
    "TRIBUTO",
    "ODONTO",
    "PENSÃO",
    "EMPRE",
    "FALTAS",
    "MENSALIDADE",
)

# Prefixos de código de rubrica que indicam dedução.
DEDUCTION_CODE_PREFIXES = (
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


def _load_totals(raw) -> dict:
    """Lê a coluna `totals` (JSON) de forma segura."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def fix_provento_classification(db) -> dict:
    """
    Corrige retroativamente a classificação das rubricas e recalcula totais.

    Varre `rubricas_holerite` e, onde o registro ainda estiver como
    'provento' mas a descrição contiver uma palavra de dedução ou o código
    começar com um prefixo de dedução, força `tipo = 'desconto'`.

    Em seguida, recalcula `total_earnings`, `total_deductions` e `net_value`
    nos `holerites` cujas rubricas foram alteradas.

    Args:
        db (sqlite3.Connection): conexão ativa com o banco.

    Returns:
        dict: {"fixed": n, "recalculated": m, "scanned": total}
    """
    # Rubricas cujo código está no catálogo oficial são decididas pelo
    # catálogo — a heurística por palavra NÃO deve sobrescrevê-las (ex.:
    # '/B01' contém 'ADIANTAMENTO' mas é PROVENTO no catálogo).
    catalog = get_catalog_map(db)

    rows = db.execute(
        "SELECT id, holerite_id, codigo, descricao, tipo FROM rubricas_holerite"
    ).fetchall()

    changed_holerite_ids = set()
    fixed = 0
    scanned = 0
    for row in rows:
        scanned += 1
        if str(row["tipo"] or "").lower() == "desconto":
            continue
        codigo = str(row["codigo"] or "").upper()
        desc = str(row["descricao"] or "").upper()
        # Itens cobertos pelo catálogo são responsabilidade do catálogo.
        if _norm_code(codigo) in catalog:
            continue
        text = f"{codigo} {desc}"
        is_deduction = any(kw in desc for kw in DEDUCTION_KEYWORDS) or any(
            prefix in text for prefix in DEDUCTION_CODE_PREFIXES
        )
        if is_deduction:
            db.execute(
                "UPDATE rubricas_holerite SET tipo='desconto' WHERE id=?",
                [row["id"]],
            )
            fixed += 1
            changed_holerite_ids.add(row["holerite_id"])

    # Recalcula os totais dos holerites afetados.
    recalculated = 0
    for hid in changed_holerite_ids:
        prov_row = db.execute(
            "SELECT COALESCE(SUM(valor), 0) AS s FROM rubricas_holerite "
            "WHERE holerite_id=? AND tipo='provento'",
            [hid],
        ).fetchone()
        desc_row = db.execute(
            "SELECT COALESCE(SUM(valor), 0) AS s FROM rubricas_holerite "
            "WHERE holerite_id=? AND tipo='desconto'",
            [hid],
        ).fetchone()
        sum_proventos = float(prov_row["s"] or 0.0)
        sum_descontos = float(desc_row["s"] or 0.0)

        holerite = db.execute(
            "SELECT totals FROM holerites WHERE id=?", [hid]
        ).fetchone()
        if holerite is None:
            continue
        totals = _load_totals(holerite["totals"])

        total_earnings = (
            sum_proventos
            if sum_proventos > 0
            else float(totals.get("total_earnings") or 0.0)
        )
        total_deductions = (
            sum_descontos
            if sum_descontos > 0
            else float(totals.get("total_deductions") or 0.0)
        )
        totals["total_earnings"] = round(total_earnings, 2)
        totals["total_deductions"] = round(total_deductions, 2)
        totals["net_value"] = round(total_earnings - total_deductions, 2)

        db.execute(
            "UPDATE holerites SET totals=? WHERE id=?",
            [json.dumps(totals, ensure_ascii=False), hid],
        )
        recalculated += 1

    db.commit()

    logger.info(
        "fix_provento_classification: varridas=%d corrigidas=%d recalculados=%d",
        scanned,
        fixed,
        recalculated,
    )
    return {"scanned": scanned, "fixed": fixed, "recalculated": recalculated}


# ---------------------------------------------------------------------
# Catálogo oficial de rubricas
# ---------------------------------------------------------------------
# Fonte de verdade determinística para PROVENTO/DESCONTO. Populado no
# startup (`init_rubrica_catalog`) e consultado na camada de enforcement
# (`deepseek_service._enforce_official_catalog`) e nas migrações.
RUBRICA_CATALOG = [
    # ---- PROVENTO ----
    ("1000", "Salário Hora", "PROVENTO"),
    ("1303", "Kit material escolar", "PROVENTO"),
    ("1334", "Adiantamento PPR", "PROVENTO"),
    ("1352", "Assistência Educacional", "PROVENTO"),
    ("1357", "Abono Indenizatório", "PROVENTO"),
    ("1360", "Diferença Salario", "PROVENTO"),
    ("1503", "Hora extra 70%", "PROVENTO"),
    ("1507", "Hora extra 100%", "PROVENTO"),
    ("1523", "Hora extra noturna 70%", "PROVENTO"),
    ("1527", "Hora extra noturna 100%", "PROVENTO"),
    ("1533", "Hora extra interjor. 70%", "PROVENTO"),
    ("1596", "Adicional noturno 35%", "PROVENTO"),
    ("1600", "Ad. Nt. 35% s/ H.E. Not.", "PROVENTO"),
    ("1604", "Redução horas Noturnas", "PROVENTO"),
    ("1606", "Ad. Noturno s/ Red Horas", "PROVENTO"),
    ("1697", "DSR S/ Horas normais", "PROVENTO"),
    ("1699", "Prêmio presenteísmo", "PROVENTO"),
    ("1703", "DSR H.E. 70%", "PROVENTO"),
    ("1707", "DSR H.E. 100%", "PROVENTO"),
    ("1723", "DSR H.E. Nt. 70%", "PROVENTO"),
    ("1727", "DSR H.E. Nt. 100%", "PROVENTO"),
    ("1733", "DSR H.E. Int. Jor. 70%", "PROVENTO"),
    ("1796", "DSR Ad. Nt. 35%", "PROVENTO"),
    ("1800", "DSR Ad. Nt. s/ HE NT 35%", "PROVENTO"),
    ("1804", "DSR S/ Redução horas Not", "PROVENTO"),
    ("1806", "DSR S/ Red Hrs. Not-Ad Not", "PROVENTO"),
    ("M200", "Férias", "PROVENTO"),
    ("MZ00", "Férias", "PROVENTO"),
    ("M389", "Provisão Contr. INSS rec.", "PROVENTO"),
    ("MC03", "Férias no mês", "PROVENTO"),
    ("MC04", "Férias 1/3 no mês", "PROVENTO"),
    ("MC13", "Médias férias no mês", "PROVENTO"),
    ("MC14", "Médias férias 1/3 no mês", "PROVENTO"),
    ("MC40", "Abono de férias reav", "PROVENTO"),
    ("MC42", "Abono de férias- 1/3 reav", "PROVENTO"),
    ("MC46", "Abono de férias- Médias reav", "PROVENTO"),
    ("MC47", "Abono férias-Médias 1/3 reav", "PROVENTO"),
    ("MCPO", "Parc/créd Prov Acumulada", "PROVENTO"),
    ("MCP0", "Parc/créd Prov Acumulada", "PROVENTO"),
    ("MZ02", "Férias 1/3", "PROVENTO"),
    ("MZ10", "Médias férias", "PROVENTO"),
    ("MZ12", "Médias férias 1/3", "PROVENTO"),
    ("MZ20", "Abono de férias", "PROVENTO"),
    ("MZ22", "Abono de férias - 1/3", "PROVENTO"),
    ("MZ26", "Abono de férias - Médias", "PROVENTO"),
    ("MZ27", "Abono férias - Médias 1/3", "PROVENTO"),
    ("/337", "Diferença de 13º salário", "PROVENTO"),
    ("/B01", "Base adiantamento salário", "PROVENTO"),
    # ---- DESCONTO ----
    ("4500", "Desc. Co-Part. Pl. Saúde", "DESCONTO"),
    ("4518", "Taxa Negocial", "DESCONTO"),
    ("4599", "Reversão Salarial", "DESCONTO"),
    ("4600", "Desc. Co-Part. Pl. Odonto", "DESCONTO"),
    ("4601", "Seguro de Vida", "DESCONTO"),
    ("4613", "Fretado", "DESCONTO"),
    ("4621", "Refeitório", "DESCONTO"),
    ("M000", "Férias imp", "DESCONTO"),
    ("MO00", "Férias - imp", "DESCONTO"),
    ("M002", "Férias 1/3 imp", "DESCONTO"),
    ("MO02", "Férias 1/3 - imp", "DESCONTO"),
    ("M010", "Médias férias imp", "DESCONTO"),
    ("MO10", "Médias férias - imp", "DESCONTO"),
    ("M012", "Médias férias 1/3 imp", "DESCONTO"),
    ("MO12", "Médias férias 1/3 - imp", "DESCONTO"),
    ("M388", "Provisão Contr. INSS ded", "DESCONTO"),
    ("MC20", "Abono de férias imp", "DESCONTO"),
    ("MC22", "Abono de férias - 1/3 imp", "DESCONTO"),
    ("MC26", "Abono férias - Médias imp", "DESCONTO"),
    ("MC27", "Abono férias- Médias 1/3 imp", "DESCONTO"),
    ("MCT0", "Parc Cred Trab Contrato 1", "DESCONTO"),
    ("MCPA", "Parc/créd Provisão ADIA", "DESCONTO"),
    ("/303", "Trib. INSS (13º)", "DESCONTO"),
    ("/314", "Contr. INSS Remuneração", "DESCONTO"),
    ("/401", "Tributo IRRF", "DESCONTO"),
    ("/B02", "Adto. Quinzenal Bruto Pg.", "DESCONTO"),
]


def _norm_code(value) -> str:
    """Normaliza um código de rubrica (maiúsculo, sem '/' inicial)."""
    return str(value or "").strip().upper().lstrip("/")


def init_rubrica_catalog(db) -> dict:
    """
    Garante a existência da tabela `catalogo_rubricas` e popula com os dados
    oficiais (upsert). Idempotente — pode rodar a cada startup.

    Args:
        db (sqlite3.Connection): conexão ativa.

    Returns:
        dict: {"seeded": n, "upserted": m}
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS catalogo_rubricas (
            codigo    VARCHAR(20) PRIMARY KEY,
            descricao VARCHAR(255) NOT NULL,
            tipo      VARCHAR(10)  NOT NULL CHECK (tipo IN ('PROVENTO', 'DESCONTO'))
        )
        """
    )
    upserted = 0
    for codigo, descricao, tipo in RUBRICA_CATALOG:
        db.execute(
            """
            INSERT INTO catalogo_rubricas (codigo, descricao, tipo)
            VALUES (?, ?, ?)
            ON CONFLICT(codigo) DO UPDATE SET
                descricao = excluded.descricao,
                tipo      = excluded.tipo
            """,
            [codigo, descricao, tipo],
        )
        upserted += 1
    db.commit()
    logger.info("init_rubrica_catalog: %d rubricas sincronizadas", upserted)
    return {"seeded": len(RUBRICA_CATALOG), "upserted": upserted}


def get_catalog_map(db) -> dict:
    """
    Retorna o catálogo como um mapa {código: tipo ('PROVENTO'/'DESCONTO')}.

    Indexa tanto pelo código exato (ex.: '/401') quanto pelo normalizado
    (sem a barra inicial, ex.: '401'), permitindo casar rubricas extraídas
    com ou sem a barra — cumprindo o casamento exato de `codigo`.
    """
    rows = db.execute("SELECT codigo, tipo FROM catalogo_rubricas").fetchall()
    catalog = {}
    for r in rows:
        tipo = str(r["tipo"]).upper()
        codigo = str(r["codigo"]).strip()
        catalog[codigo] = tipo          # casamento exato (com barra, maiúsculo)
        catalog[_norm_code(codigo)] = tipo  # casamento normalizado
    return catalog


def fix_classification_from_catalog(db) -> dict:
    """
    Migração determinística: força o `tipo` de cada rubrica a partir do
    catálogo oficial e recalcula os totais dos holerites alterados.

    Corrige dados históricos gravados com classificação errada (ex.: '/B01'
    marcado como desconto, ou '/401'/'MCPA' marcados como provento).

    Args:
        db (sqlite3.Connection): conexão ativa.

    Returns:
        dict: {"scanned", "fixed", "recalculated"}
    """
    init_rubrica_catalog(db)
    catalog = get_catalog_map(db)

    rows = db.execute(
        "SELECT id, holerite_id, codigo, descricao, tipo FROM rubricas_holerite"
    ).fetchall()

    changed_holerite_ids = set()
    fixed = 0
    scanned = 0
    for row in rows:
        scanned += 1
        norm = _norm_code(row["codigo"])
        official_tipo = catalog.get(norm)
        if official_tipo is None:
            continue
        target = official_tipo.lower()  # 'provento' | 'desconto'
        current = str(row["tipo"] or "").lower()
        if current == target:
            continue
        db.execute(
            "UPDATE rubricas_holerite SET tipo=? WHERE id=?",
            [target, row["id"]],
        )
        fixed += 1
        changed_holerite_ids.add(row["holerite_id"])

    recalculated = 0
    for hid in changed_holerite_ids:
        prov = db.execute(
            "SELECT COALESCE(SUM(ABS(valor)), 0) AS s FROM rubricas_holerite "
            "WHERE holerite_id=? AND tipo='provento'",
            [hid],
        ).fetchone()["s"]
        desc = db.execute(
            "SELECT COALESCE(SUM(ABS(valor)), 0) AS s FROM rubricas_holerite "
            "WHERE holerite_id=? AND tipo='desconto'",
            [hid],
        ).fetchone()["s"]
        sum_proventos = float(prov or 0.0)
        sum_descontos = float(desc or 0.0)

        holerite = db.execute(
            "SELECT totals FROM holerites WHERE id=?", [hid]
        ).fetchone()
        if holerite is None:
            continue
        totals = _load_totals(holerite["totals"])
        total_earnings = (
            sum_proventos
            if sum_proventos > 0
            else float(totals.get("total_earnings") or 0.0)
        )
        total_deductions = (
            sum_descontos
            if sum_descontos > 0
            else float(totals.get("total_deductions") or 0.0)
        )
        totals["total_earnings"] = round(total_earnings, 2)
        totals["total_deductions"] = round(total_deductions, 2)
        totals["net_value"] = round(total_earnings - total_deductions, 2)
        db.execute(
            "UPDATE holerites SET totals=? WHERE id=?",
            [json.dumps(totals, ensure_ascii=False), hid],
        )
        recalculated += 1

    # [Defensivo] Algumas variantes de schema gravam as rubricas também como
    # JSON na coluna `holerites.line_items`. Se essa coluna existir, reclassifica
    # cada item in-place (campo `tipo`/`type`) conforme o catálogo e persiste.
    json_fixed = 0
    cols = {r["name"] for r in db.execute("PRAGMA table_info(holerites)").fetchall()}
    if "line_items" in cols:
        hrows = db.execute("SELECT id, line_items FROM holerites WHERE line_items IS NOT NULL").fetchall()
        for h in hrows:
            try:
                items = json.loads(h["line_items"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(items, list):
                continue
            changed = False
            for it in items:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("codigo") or it.get("code") or "").strip().upper()
                norm = code.lstrip("/")
                t = catalog.get(code) or catalog.get(norm)
                if t is None:
                    continue
                key = it.get("tipo") if "tipo" in it else it.get("type")
                if str(key or "").upper() != t:
                    it["tipo"] = t.lower()   # padrão do schema relacional
                    it["type"] = t            # padrão do contrato
                    changed = True
            if changed:
                db.execute(
                    "UPDATE holerites SET line_items=? WHERE id=?",
                    [json.dumps(items, ensure_ascii=False), h["id"]],
                )
                json_fixed += 1

    db.commit()
    logger.info(
        "fix_classification_from_catalog: varridas=%d corrigidas=%d recalculados=%d json=%d",
        scanned,
        fixed,
        recalculated,
        json_fixed,
    )
    return {
        "scanned": scanned,
        "fixed": fixed,
        "recalculated": recalculated,
        "json_fixed": json_fixed,
    }


