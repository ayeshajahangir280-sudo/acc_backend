"""PDF / email actions mixed into the sales and purchase viewsets and the party viewset.

They resolve the document through the viewset's own (organization- and branch-scoped) queryset, so an out-of-scope
document is a 404 here exactly as it is everywhere else. Emailing is a POST and therefore needs a write role.
"""
import re
from datetime import date

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import DomainError
from apps.organizations.access import request_scope

from . import mail, pdf as pdfmod
from .statement import build_statement


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def _party(p):
    return {"name": p.name, "address": p.address, "tax_number": p.tax_number, "email": p.email}


def _org(o):
    return {"org_name": o.name, "org_legal": o.legal_name, "currency": o.base_currency}


class _EmailIn(serializers.Serializer):
    to = serializers.CharField(required=False, allow_blank=True, max_length=500)
    subject = serializers.CharField(required=False, allow_blank=True, max_length=200)
    message = serializers.CharField(required=False, allow_blank=True, max_length=5000)


def _pdf_response(data, filename):
    r = HttpResponse(data, content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{filename}"'
    r["X-Content-Type-Options"] = "nosniff"
    return r


class DocumentPdfMixin:
    """For invoice-like viewsets. Needs `doc_side` = 'sales' | 'purchases'."""

    doc_side = None

    def _doc_data(self, inv):
        sales = self.doc_side == "sales"
        is_return = inv.kind == "return"
        title = ("Credit Note" if sales else "Vendor Credit") if is_return else ("Invoice" if sales else "Bill")
        meta = [("Number", inv.number), ("Date", inv.date)]
        if inv.due_date and not is_return:
            meta.append(("Due date", inv.due_date))
        d = {**_org(self.request.organization), "title": title, "number": inv.number, "meta": meta,
             "party_label": "Bill to" if sales else "Supplier", "party": _party(inv.party),
             "items": list(inv.items.all().order_by("id").values("description", "quantity", "unit_price", "tax_rate", "net", "tax", "gross")),
             "subtotal": inv.subtotal, "tax_total": inv.tax_total, "total": inv.total, "narration": inv.narration,
             "void": inv.status == "void", "balance_due": None, "reference": None}
        if is_return and inv.original_id:
            d["reference"] = f"Against {'invoice' if sales else 'bill'} {inv.original.number}"
        if not is_return and inv.status == "posted":
            from apps.treasury.selectors import outstanding_invoices

            row = outstanding_invoices(self.request.organization, "receipt" if sales else "payment", invoice_ids=[inv.pk]).first()
            d["balance_due"] = row.outstanding if row else None
        return d

    def _render(self):
        inv = self.get_object()
        inv = type(inv).objects.select_related("party", "original").get(pk=inv.pk)
        d = self._doc_data(inv)
        return inv, d, pdfmod.document_pdf(d), f"{_safe(d['title'].replace(' ', '-'))}-{_safe(inv.number)}.pdf"

    @action(detail=True, methods=["get"], url_path="pdf")
    def pdf(self, request, pk=None):
        _, _, data, name = self._render()
        return _pdf_response(data, name)

    @action(detail=True, methods=["post"], url_path="email")
    def email(self, request, pk=None):
        ser = _EmailIn(data=request.data)
        ser.is_valid(raise_exception=True)
        inv, d, data, name = self._render()
        if inv.status == "void":
            raise DomainError("A void document cannot be emailed.", "void_document")
        if inv.status in ("pending", "rejected"):
            raise DomainError("A document that is not approved cannot be emailed.", "not_approved")
        v = ser.validated_data
        to = v.get("to") or inv.party.email
        subject = v.get("subject") or f"{d['title']} {inv.number} from {d['org_name']}"
        body = v.get("message") or f"Hello {inv.party.name},\n\nPlease find {d['title'].lower()} {inv.number} attached.\n\n{d['org_name']}"
        log = mail.send_pdf(request.organization, request.user, doc_type=f"{self.doc_side}.invoice", doc_id=inv.pk, to=to,
                            subject=subject, body=body, filename=name, pdf=data)
        return Response({"sent_to": log.to, "subject": log.subject})


class StatementMixin:
    """For PartyViewSet: /parties/{id}/statement/ (JSON), /statement-pdf/, POST /statement-email/."""

    def _statement(self, request):
        party = self.get_object()
        side = request.query_params.get("side") or request.data.get("side") or ("purchases" if party.kind == "supplier" else "sales")
        if side not in ("sales", "purchases"):
            raise serializers.ValidationError({"side": "Use sales or purchases."})
        if (side == "sales" and party.kind == "supplier") or (side == "purchases" and party.kind == "customer"):
            raise serializers.ValidationError({"side": "This party has no such transactions."})
        f = serializers.DateField(required=False, allow_null=True)
        src = request.data if request.method == "POST" else request.query_params
        date_from = f.run_validation(src.get("date_from")) if src.get("date_from") else None
        date_to = f.run_validation(src.get("date_to")) if src.get("date_to") else timezone.localdate()
        if date_from and date_from > date_to:
            raise serializers.ValidationError({"date_from": "Must not be after the end date."})
        st = build_statement(request.organization, party, side, date_from, date_to, request_scope(request))
        sales = side == "sales"
        data = {**_org(request.organization), "title": "Statement of Account", "party": _party(party), "date_from": date_from,
                "date_to": date_to, "number": "", "label_debit": "Charges" if sales else "Payments", "label_credit": "Payments" if sales else "Bills", **st}
        return party, side, data

    @action(detail=True, methods=["get"], url_path="statement")
    def statement(self, request, pk=None):
        party, side, d = self._statement(request)
        return Response({"party": party.id, "side": side, "date_from": d["date_from"], "date_to": d["date_to"], "opening": d["opening"],
                         "rows": d["rows"], "closing": d["closing"]})

    @action(detail=True, methods=["get"], url_path="statement-pdf")
    def statement_pdf(self, request, pk=None):
        party, side, d = self._statement(request)
        return _pdf_response(pdfmod.statement_pdf(d), f"Statement-{_safe(party.name)}.pdf")

    @action(detail=True, methods=["post"], url_path="statement-email")
    def statement_email(self, request, pk=None):
        ser = _EmailIn(data=request.data)
        ser.is_valid(raise_exception=True)
        party, side, d = self._statement(request)
        v = ser.validated_data
        org = request.organization
        log = mail.send_pdf(org, request.user, doc_type=f"{side}.statement", doc_id=party.pk, to=v.get("to") or party.email,
                            subject=v.get("subject") or f"Statement of account from {org.name}",
                            body=v.get("message") or f"Hello {party.name},\n\nYour statement of account is attached.\n\n{org.name}",
                            filename=f"Statement-{_safe(party.name)}.pdf", pdf=pdfmod.statement_pdf(d))
        return Response({"sent_to": log.to, "subject": log.subject})
