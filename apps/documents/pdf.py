"""PDF rendering (reportlab). Pure functions: data in, bytes out. No database access here."""
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

SS = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=SS["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8, textColor=colors.grey)
H1 = ParagraphStyle("h1", parent=SS["Heading1"], fontSize=18, spaceAfter=2)
RIGHT = ParagraphStyle("right", parent=BODY, alignment=2)


def _m(v):
    return f"{Decimal(str(v or 0)):,.2f}"


def _q(v):
    d = Decimal(str(v or 0)).normalize()
    return f"{d:f}"


def _p(text, style=BODY):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def _build(story, title, void=False):
    buf = BytesIO()

    def deco(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 10 * mm, title)
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
        if void:
            canvas.setFont("Helvetica-Bold", 90)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.18))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "VOID")
        canvas.restoreState()

    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
                      title=title, author="").build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def _head(d):
    left = [Paragraph(escape(d["org_name"]), H1)]
    if d.get("org_legal") and d["org_legal"] != d["org_name"]:
        left.append(_p(d["org_legal"], SMALL))
    right = [_p(d["title"].upper(), ParagraphStyle("t", parent=H1, alignment=2, fontSize=16, textColor=colors.HexColor("#334155")))]
    for k, v in d["meta"]:
        right.append(Paragraph(f"<b>{escape(k)}:</b> {escape(str(v))}", RIGHT))
    t = Table([[left, right]], colWidths=[95 * mm, 85 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _party(label, p):
    lines = [f"<b>{escape(label)}</b>", escape(p["name"])]
    for k in ("address", "tax_number", "email"):
        if p.get(k):
            lines.append(escape(("Tax no. " if k == "tax_number" else "") + p[k]).replace("\n", "<br/>"))
    return Paragraph("<br/>".join(lines), BODY)


def _grid(rows, widths, right_cols):
    t = Table(rows, colWidths=widths, repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
          ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
          ("VALIGN", (0, 0), (-1, -1), "TOP")]
    st += [("ALIGN", (c, 0), (c, -1), "RIGHT") for c in right_cols]
    t.setStyle(TableStyle(st))
    return t


def document_pdf(d):
    """d: org_name, org_legal, currency, title, meta[(k,v)], party_label, party{}, items[], subtotal, tax_total, total,
    balance_due (None to omit), narration, void(bool), reference (e.g. original invoice number)."""
    story = [_head(d), Spacer(1, 6 * mm), _party(d["party_label"], d["party"]), Spacer(1, 6 * mm)]
    rows = [["Description", "Qty", "Unit price", "Tax %", "Net", "Tax", "Total"]]
    for i in d["items"]:
        rows.append([_p(i["description"]), _q(i["quantity"]), _m(i["unit_price"]), _q(i["tax_rate"]), _m(i["net"]), _m(i["tax"]), _m(i["gross"])])
    story.append(_grid(rows, [62 * mm, 16 * mm, 22 * mm, 13 * mm, 22 * mm, 20 * mm, 25 * mm], [1, 2, 3, 4, 5, 6]))
    story.append(Spacer(1, 4 * mm))
    tot = [["Subtotal", f"{d['currency']} {_m(d['subtotal'])}"], ["Tax", f"{d['currency']} {_m(d['tax_total'])}"],
           ["Total", f"{d['currency']} {_m(d['total'])}"]]
    if d.get("balance_due") is not None:
        tot.append(["Balance due", f"{d['currency']} {_m(d['balance_due'])}"])
    t = Table(tot, colWidths=[35 * mm, 40 * mm], hAlign="RIGHT")
    t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9),
                           ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"), ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.black)]))
    story.append(t)
    if d.get("reference"):
        story += [Spacer(1, 4 * mm), _p(d["reference"], SMALL)]
    if d.get("narration"):
        story += [Spacer(1, 4 * mm), _p("Notes", SMALL), _p(d["narration"])]
    return _build(story, f"{d['title']} {d['number']}", d.get("void", False))


def statement_pdf(s):
    """s: org_name, org_legal, currency, title, party{}, date_from, date_to, opening, rows[(date, ref, narration, debit, credit, balance)],
    closing, label_debit, label_credit."""
    meta = [("Statement date", s["date_to"]), ("Period", f"{s['date_from'] or 'start'} to {s['date_to']}")]
    story = [_head({**s, "meta": meta}), Spacer(1, 6 * mm), _party("Statement for", s["party"]), Spacer(1, 6 * mm)]
    rows = [["Date", "Reference", "Details", s["label_debit"], s["label_credit"], "Balance"],
            ["", "", _p("Opening balance"), "", "", _m(s["opening"])]]
    for r in s["rows"]:
        rows.append([str(r["date"]), _p(r["reference"]), _p(r["narration"]), _m(r["debit"]) if r["debit"] else "",
                     _m(r["credit"]) if r["credit"] else "", _m(r["balance"])])
    story.append(_grid(rows, [22 * mm, 30 * mm, 56 * mm, 24 * mm, 24 * mm, 24 * mm], [3, 4, 5]))
    story += [Spacer(1, 4 * mm), Paragraph(f"<b>Closing balance: {escape(s['currency'])} {_m(s['closing'])}</b>", RIGHT)]
    if not s["rows"]:
        story += [Spacer(1, 3 * mm), _p("No transactions in this period.", SMALL)]
    return _build(story, f"Statement {s['party']['name']}")


def reconciliation_pdf(r):
    """r: org_name, title, head[(label, value)], rows[{date, description, reference, amount, status, matched_to}]."""
    story = [Paragraph(escape(r["org_name"]), H1), _p(r["title"]), Spacer(1, 4 * mm)]
    t = Table([[_p(k, SMALL), _p(v)] for k, v in r["head"]], colWidths=[55 * mm, 80 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, Spacer(1, 5 * mm)]
    rows = [["Date", "Description", "Reference", "Amount", "Status", "Matched to"]]
    for x in r["rows"]:
        rows.append([str(x["date"]), _p(x["description"]), _p(x["reference"]), _m(x["amount"]), x["status"], _p(x["matched_to"])])
    story.append(_grid(rows, [20 * mm, 50 * mm, 25 * mm, 24 * mm, 20 * mm, 41 * mm], [3]))
    return _build(story, r["title"])
