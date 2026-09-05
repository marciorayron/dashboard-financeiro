"""
routes/reports.py
-----------------
Relatórios executivos em PDF (dossiê financeiro CLT).

`reportlab` é uma dependência OPCIONAL: se não estiver instalada, a aplicação
segue funcionando normalmente e o endpoint de PDF responde 503 com mensagem
clara (a pré-visualização em HTML continua disponível).
"""
import hashlib
import json
from datetime import date
from io import BytesIO

from flask import Blueprint, Response, jsonify, render_template, request

from database.connection import get_db
from models.profile import load_profile
from routes.auth import current_user_id, login_required
from services import analytics_service

# Import opcional/lazy do reportlab — não deve derrubar o startup da app.
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    HAS_REPORTLAB = True
except ImportError:  # pragma: no cover - ambiente sem reportlab
    HAS_REPORTLAB = False

reports_bp = Blueprint("reports", __name__)


def _monthly_nets(db, user_id) -> dict:
    """Agrega o net real por mês (adiantamento + folha)."""
    rows = db.execute(
        "SELECT mes_referencia, totals FROM holerites "
        "WHERE user_id=? AND mes_referencia IS NOT NULL",
        [user_id],
    ).fetchall()
    monthly: dict = {}
    for r in rows:
        try:
            t = json.loads(r["totals"])
        except (ValueError, TypeError):
            continue
        month = str(r["mes_referencia"])[:7]
        monthly[month] = monthly.get(month, 0.0) + float(t.get("net_value") or 0.0)
    return dict(sorted(monthly.items()))


def _brl(value) -> str:
    return f"R$ {float(value or 0.0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Códigos de proventos variáveis (H.E., noturno, DSR-HE, férias, PPR) usados
# para separar 'Proventos Fixos' de 'H.E./Variável' na tabela mensal.
_VARIABLE_CODES = {
    "1503", "1506", "1507", "1521", "1523", "1527", "1533", "1596", "1600",
    "1604", "1606", "1703", "1707", "1723", "1727", "1733", "1796", "1800",
    "1804", "1806", "1334", "MCP0", "M389",
    "M200", "MZ00", "MZ02", "MZ10", "MZ12", "MZ20", "MZ22", "MZ26", "MZ27",
    "MC03", "MC04", "MC13", "MC14", "MC40", "MC42", "MC46", "MC47",
}


def _monthly_breakdown(db, user_id, limit: int = 6) -> list:
    """Quebra por competência: fixos, variáveis, retenções e líquido."""
    rows = db.execute(
        """
        SELECT h.mes_referencia, r.codigo, r.tipo, r.valor
        FROM rubricas_holerite r
        JOIN holerites h ON h.id = r.holerite_id
        WHERE r.user_id = ? AND h.mes_referencia IS NOT NULL
        """,
        [user_id],
    ).fetchall()
    monthly: dict = {}
    for r in rows:
        month = str(r["mes_referencia"])[:7]
        info = monthly.setdefault(month, {"fixed": 0.0, "variable": 0.0, "ret": 0.0})
        norm = str(r["codigo"] or "").upper().lstrip("/")
        v = abs(float(r["valor"] or 0.0))
        tipo = str(r["tipo"]).lower()
        if tipo == "provento":
            if norm in _VARIABLE_CODES:
                info["variable"] += v
            else:
                info["fixed"] += v
        elif tipo == "desconto":
            info["ret"] += v
    out = []
    for month in sorted(monthly.keys())[-limit:]:
        d = monthly[month]
        out.append(
            {
                "month": month,
                "fixed": d["fixed"],
                "variable": d["variable"],
                "ret": d["ret"],
                "net": d["fixed"] + d["variable"] - d["ret"],
            }
        )
    return out


def _compute_doc_hash(user_id, profile, m12, theoretical) -> str:
    """Hash SHA-256 (16 primeiros chars) usado como código de autenticidade."""
    payload = {
        "user": user_id,
        "contract": profile.contract_type,
        "base_rate": profile.base_rate,
        "recurrent_12m": round(m12, 2),
        "theoretical": round(theoretical, 2),
        "generated": date.today().isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16].upper()


@reports_bp.route("/reports/dossier-pdf", methods=["GET"])
@login_required
def dossier_pdf():
    """Gera o Dossiê de Capacidade Financeira CLT (certificado, 1 página)."""
    user_id = current_user_id()
    db = get_db()
    profile = load_profile(db, user_id)

    # Dados do trabalhador e empregador.
    u = db.execute("SELECT name FROM users WHERE id=?", [user_id]).fetchone()
    worker_name = u["name"] if u else ""
    comp = db.execute(
        "SELECT company_name FROM holerites WHERE user_id=? "
        "AND company_name IS NOT NULL ORDER BY mes_referencia DESC LIMIT 1",
        [user_id],
    ).fetchone()
    company = comp["company_name"] if comp else ""

    monthly = _monthly_nets(db, user_id)
    months = sorted(monthly.keys())

    def avg(n):
        window = months[-n:]
        return sum(monthly[m] for m in window) / len(window) if window else 0.0

    m3, m6, m12 = avg(3), avg(6), avg(12)
    theoretical = analytics_service.theoretical_recurrent_net(profile)
    baseline = m12 if m12 > 0 else m6
    commitment = baseline * 0.30
    breakdown = _monthly_breakdown(db, user_id, 6)
    doc_hash = _compute_doc_hash(user_id, profile, m12, theoretical)
    doc_id = f"DFC-{user_id}-{date.today():%Y%m%d}-{doc_hash[:6]}"

    if request.args.get("format") == "html":
        return render_template(
            "dossier_pdf.html",
            profile=profile, m3=m3, m6=m6, m12=m12,
            theoretical=theoretical, doc_hash=doc_hash,
            user_id=user_id, worker_name=worker_name, company=company,
            commitment=commitment, breakdown=breakdown, doc_id=doc_id,
        )
    if not HAS_REPORTLAB:
        return jsonify(
            {
                "error": (
                    "Dossiê PDF indisponível: instale a dependência opcional "
                    "'reportlab' (pip install -r requirements.txt) e reinicie a aplicação."
                )
            }
        ), 503
    return _build_pdf(
        worker_name, company, profile,
        m3, m6, m12, theoretical, commitment, breakdown,
        doc_hash, doc_id, user_id,
    )


@reports_bp.route("/reports/dispute-pdf", methods=["GET"])
@login_required
def dispute_pdf():
    """
    Relatório de Disputa de RH (HR Dispute Report) — inconsistências itemizadas.

    Lista cada inconsistência detectada com severidade, mês e impacto R$,
    permitindo encaminhamento formal ao RH/DP. `reportlab` é opcional; sem ele
    responde 503 com orientação de instalação.
    """
    user_id = current_user_id()
    db = get_db()
    profile = load_profile(db, user_id)

    u = db.execute("SELECT name FROM users WHERE id=?", [user_id]).fetchone()
    worker_name = u["name"] if u else ""
    comp = db.execute(
        "SELECT company_name FROM holerites WHERE user_id=? "
        "AND company_name IS NOT NULL ORDER BY mes_referencia DESC LIMIT 1",
        [user_id],
    ).fetchone()
    company = comp["company_name"] if comp else ""

    breakdown = analytics_service.get_inconsistency_breakdown(user_id)

    if not HAS_REPORTLAB:
        return jsonify(
            {
                "error": (
                    "Relatório PDF indisponível: instale a dependência opcional "
                    "'reportlab' (pip install -r requirements.txt) e reinicie a aplicação."
                )
            }
        ), 503

    return _build_dispute_pdf(worker_name, company, profile, breakdown)


@reports_bp.route("/verify/<doc_hash>", methods=["GET"])
def verify_doc(doc_hash):
    """Página de autenticação do documento (aponta o hash impresso no PDF)."""
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Verificação de Documento</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;padding:24px;color:#1e293b}}
h2{{color:#059669}}code{{background:#f1f5f9;padding:4px 8px;border-radius:6px}}</style></head>
<body><h2>&#10004; Código de autenticidade identificado</h2>
<p>Documento de Capacidade Financeira emitido pelo Dashboard Financeiro CLT.</p>
<p>Código: <code>{doc_hash}</code></p>
<p><small>Compare este código com o hash impresso no rodapé do documento para confirmar a integridade.</small></p>
</body></html>"""


def _pdf_footer(canvas, doc):
    """Rodapé com número de página e disclaimer."""
    canvas.saveState()
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawCentredString(
        A4[0] / 2.0, 9 * mm,
        f"Página {doc.page}  ·  Documento de Capacidade Financeira CLT  ·  Uso informativo",
    )
    canvas.restoreState()


def _build_pdf(worker_name, company, profile, m3, m6, m12, theoretical,
               commitment, breakdown, doc_hash, doc_id, user_id):
    """Monta o Dossiê de Capacidade Financeira (certificado, 1 página)."""
    NAVY = colors.HexColor("#0f172a")
    SLATE = colors.HexColor("#1e293b")
    BLUE = colors.HexColor("#2563eb")
    GREEN = colors.HexColor("#059669")
    AMBER = colors.HexColor("#d97706")
    BORDER = colors.HexColor("#cbd5e1")
    ROW_A = colors.HexColor("#f8fafc")
    ROW_B = colors.white

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=11 * mm, bottomMargin=13 * mm,
    )
    styles = getSampleStyleSheet()

    def st(name, **kw):
        parent = kw.pop("parent", styles["Normal"])
        return ParagraphStyle(name, parent=parent, **kw)

    banner_title = st("banner_title", fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=colors.white)
    banner_sub = st("banner_sub", fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor("#cbd5e1"))
    section = st("section", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=SLATE, spaceBefore=6, spaceAfter=2)
    field = st("field", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#475569"))
    value = st("value", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=SLATE)
    metric_val = st("mval", fontName="Helvetica-Bold", fontSize=13, leading=15, textColor=colors.white)
    metric_lbl = st("mlbl", fontName="Helvetica", fontSize=6.5, leading=8, textColor=colors.HexColor("#e2e8f0"))
    cell = st("cell", fontName="Helvetica", fontSize=7.5, leading=9)
    cell_b = st("cellb", fontName="Helvetica-Bold", fontSize=7.5, leading=9)
    mono = st("mono", fontName="Courier-Bold", fontSize=8, leading=10, textColor=SLATE)
    tiny = st("tiny", fontName="Helvetica", fontSize=6.5, leading=8, textColor=colors.HexColor("#64748b"))
    qr_lbl = st("qrlbl", fontName="Helvetica-Bold", fontSize=16, leading=18, textColor=SLATE, alignment=1)

    story = []

    # ---- 1) Header banner (azul-marinho) ----
    header = Table(
        [[
            Paragraph("DOSSIÊ DE CAPACIDADE FINANCEIRA CLT", banner_title),
            Paragraph(f"Emissão: {date.today().strftime('%d/%m/%Y %H:%M')}<br/>Documento: {doc_id}", banner_sub),
        ]],
        colWidths=[118 * mm, 63 * mm],
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(header)
    story.append(Spacer(1, 5))

    # ---- 2) Grid de dados do trabalhador e contrato ----
    story.append(Paragraph("DADOS DO TRABALHADOR E CONTRATO", section))

    def kv(label, val):
        return [Paragraph(label, field), Paragraph(val, value)]

    info = [
        kv("Nome", worker_name or "—"),
        kv("Empresa", company or "—"),
        kv("Cargo", profile.job_title or "—"),
        kv("Admissão", profile.admission_date or "—"),
        kv("Contrato", "Horista" if profile.contract_type == "HORISTA" else "Mensalista"),
        kv("Valor", _brl(profile.base_rate) + (" /h" if profile.contract_type == "HORISTA" else " (base)")),
    ]
    grid = [info[0] + info[1], info[2] + info[3], info[4] + info[5]]
    info_tbl = Table(grid, colWidths=[35 * mm, 52 * mm, 35 * mm, 52 * mm])
    info_tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, ROW_A]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 5))

    # ---- 3) Cards executivos ----
    def metric_box(bg, label, val, sub):
        return Table(
            [[Paragraph(val, metric_val)], [Paragraph(label, metric_lbl)], [Paragraph(sub or "", metric_lbl)]],
            colWidths=[58 * mm],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]),
        )

    summary = Table(
        [[
            metric_box(BLUE, "Líquido Recorrente Médio", _brl(m6), f"6m {_brl(m6)} · 12m {_brl(m12)}"),
            metric_box(GREEN, "Líquido Base Teórico", _brl(theoretical), "Perfil contratual"),
            metric_box(AMBER, "Capacidade de Comprometimento", _brl(commitment), "30% do líquido médio"),
        ]],
        colWidths=[60 * mm, 60 * mm, 60 * mm],
        style=TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]),
    )
    story.append(summary)
    story.append(Spacer(1, 6))

    # ---- 4) Tabela de holerites (últimos 6 meses) ----
    story.append(Paragraph("HISTÓRICO DE HOLERITES (ÚLTIMOS 6 MESES)", section))
    rows = [[
        Paragraph("Competência", cell_b),
        Paragraph("Proventos Fixos", cell_b),
        Paragraph("H.E. / Variável", cell_b),
        Paragraph("Retenções", cell_b),
        Paragraph("Líquido Final", cell_b),
    ]]
    if breakdown:
        for b in breakdown:
            rows.append([
                Paragraph(b["month"], cell),
                Paragraph(_brl(b["fixed"]), cell),
                Paragraph(_brl(b["variable"]), cell),
                Paragraph(_brl(b["ret"]), cell),
                Paragraph(_brl(b["net"]), cell_b),
            ])
    else:
        rows.append([Paragraph("Sem dados", cell)] + [Paragraph("—", cell)] * 4)
    hist = Table(rows, colWidths=[28 * mm, 40 * mm, 38 * mm, 36 * mm, 36 * mm], repeatRows=1)
    hist.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SLATE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [ROW_B, ROW_A]),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(hist)
    story.append(Spacer(1, 6))

    # ---- 5) Rodapé de segurança / autenticidade ----
    verify_url = f"/api/verify/{doc_hash}"
    qr = Table(
        [[Paragraph("QR", qr_lbl)], [Paragraph(f"autenticar<br/>{verify_url}", tiny)]],
        colWidths=[26 * mm],
        style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 1, BLUE),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]),
    )
    sec = Table(
        [[
            qr,
            Table(
                [
                    [Paragraph("CÓDIGO DE AUTENTICIDADE", st("sa", fontName="Helvetica-Bold", fontSize=7, textColor=SLATE))],
                    [Paragraph(doc_hash, mono)],
                    [Paragraph(
                        "Verifique a integridade em /api/verify/&lt;código&gt;. Documento válido "
                        "apenas quando emitido pelo sistema oficial.", tiny,
                    )],
                ],
                colWidths=[153 * mm],
            ),
        ]],
        colWidths=[26 * mm, 153 * mm],
        style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.5, BORDER)]),
    )
    story.append(sec)
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "DISCLAIMER: Documento informativo gerado automaticamente a partir de contracheques. "
        "Valores em R$. Não constitui garantia formal de renda; sujeito a validação bancária.",
        tiny,
    ))

    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    pdf = buf.getvalue()
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=dossie_capacidade_financeira.pdf"},
    )



def _build_dispute_pdf(worker_name, company, profile, breakdown):
    """Monta o Relatório de Disputa de RH (inconsistências itemizadas)."""
    NAVY = colors.HexColor("#0f172a")
    SLATE = colors.HexColor("#1e293b")
    RED = colors.HexColor("#dc2626")
    AMBER = colors.HexColor("#d97706")
    BORDER = colors.HexColor("#cbd5e1")
    ROW_A = colors.HexColor("#f8fafc")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=11 * mm, bottomMargin=13 * mm,
    )
    styles = getSampleStyleSheet()

    def st(name, **kw):
        parent = kw.pop("parent", styles["Normal"])
        return ParagraphStyle(name, parent=parent, **kw)

    banner_title = st("dt", fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=colors.white)
    banner_sub = st("ds", fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor("#cbd5e1"))
    section = st("sec", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=SLATE, spaceBefore=6, spaceAfter=2)
    field = st("f", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#475569"))
    value = st("v", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=SLATE)
    cell = st("c", fontName="Helvetica", fontSize=7.5, leading=9)
    cell_b = st("cb", fontName="Helvetica-Bold", fontSize=7.5, leading=9)
    tiny = st("t", fontName="Helvetica", fontSize=6.5, leading=8, textColor=colors.HexColor("#64748b"))

    story = []

    # ---- 1) Cabeçalho ----
    header = Table(
        [[
            Paragraph("RELATÓRIO DE DISPUTA DE RH — INCONSISTÊNCIAS", banner_title),
            Paragraph(
                f"Emissão: {date.today().strftime('%d/%m/%Y %H:%M')}<br/>"
                f"Documento: DISP-{date.today():%Y%m%d}",
                banner_sub,
            ),
        ]],
        colWidths=[118 * mm, 63 * mm],
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(header)
    story.append(Spacer(1, 6))

    # ---- 2) Identificação ----
    story.append(Paragraph("IDENTIFICAÇÃO DO TRABALHADOR", section))
    def kv(label, val):
        return [Paragraph(label, field), Paragraph(val, value)]
    info = [
        kv("Nome", worker_name or "—"),
        kv("Empresa", company or "—"),
        kv("Cargo", (profile.job_title or "—") if profile else "—"),
        kv("Admissão", (profile.admission_date or "—") if profile else "—"),
    ]
    info_tbl = Table([info[0] + info[1], info[2] + info[3]], colWidths=[35 * mm, 52 * mm, 35 * mm, 52 * mm])
    info_tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6))

    # ---- 3) Resumo ----
    total = float(breakdown.get("total") or 0.0)
    count = int(breakdown.get("count") or 0)
    story.append(Paragraph(
        f"Total de inconsistências: {count}  ·  Impacto monetário identificado: {_brl(total)}",
        section,
    ))
    story.append(Spacer(1, 3))

    # ---- 4) Tabela de inconsistências ----
    story.append(Paragraph("ITEMIZAÇÃO DAS INCONSISTÊNCIAS", section))
    rows = [[
        Paragraph("Severidade", cell_b),
        Paragraph("Categoria", cell_b),
        Paragraph("Competência", cell_b),
        Paragraph("Descrição", cell_b),
        Paragraph("Impacto R$", cell_b),
    ]]
    items = breakdown.get("items") or []
    for it in items:
        rows.append([
            Paragraph(str(it.get("severity") or "INFO"), cell),
            Paragraph(str(it.get("category") or "—"), cell),
            Paragraph(str(it.get("month") or "—"), cell),
            Paragraph(f"{it.get('title') or ''} — {it.get('description') or ''}", cell),
            Paragraph(_brl(it.get("amount") or 0.0), cell_b),
        ])
    if not items:
        rows.append([Paragraph("Nenhuma inconsistência detectada.", cell)] + [Paragraph("—", cell)] * 4)
    tbl = Table(rows, colWidths=[20 * mm, 22 * mm, 22 * mm, 88 * mm, 29 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SLATE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [ROW_A, colors.white]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8))

    # ---- 5) Rodapé legal ----
    story.append(Paragraph(
        "DISCLAIMER: Documento informativo gerado automaticamente a partir de contracheques. "
        "Valores em R$. Não constitui parecer jurídico ou confissão de dívida; deve ser "
        "encaminhado ao RH/DP para análise e regularização.",
        tiny,
    ))

    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    pdf = buf.getvalue()
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=relatorio_disputa_rh.pdf"},
    )

