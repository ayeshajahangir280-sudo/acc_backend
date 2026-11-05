"""Extra actions of the bank-statement viewset: rule suggestions, create-from-line and the reconciliation report export."""
import csv
import io

from django.http import HttpResponse
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import DomainError
from apps.ledger.models import Account
from apps.organizations.models import Branch

from . import rules, services


def _csv_safe(v):
    """Spreadsheet formula-injection guard for text that came from a bank file."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not s.replace(".", "", 1).lstrip("-").isdigit() else s


class StatementExtras:
    @action(detail=True, methods=["get"], url_path="suggestions")
    def suggestions(self, request, pk=None):
        return Response(rules.suggestions(self.get_object()))

    @action(detail=True, methods=["post"], url_path="create-from-line")
    def create_from_line(self, request, pk=None):
        from .views import _dec

        st = self.get_object()
        d = request.data
        try:
            category = Account.objects.get(pk=int(d.get("category")), organization=request.organization)
        except (TypeError, ValueError, Account.DoesNotExist):
            raise DomainError("Choose a category account.", "invalid_account")
        branch = None
        if d.get("branch") not in (None, ""):
            try:
                branch = Branch.objects.get(pk=int(d["branch"]), organization=request.organization, is_active=True)
            except (TypeError, ValueError, Branch.DoesNotExist):
                raise DomainError("Branch not found.", "invalid_branch")
        try:
            line_id = int(d.get("line"))
        except (TypeError, ValueError):
            raise DomainError("Choose a statement line.", "line_not_found")
        doc, m = rules.create_from_line(request.organization, request.user, st, line_id=line_id, category=category,
                                        tax_rate=_dec(d.get("tax_rate") or 0, "tax_rate"), narration=d.get("narration"), branch=branch)
        return Response({"document": doc.number, "journal": doc.journal_id, "match": m.id, "amount": m.amount}, status=201)

    @action(detail=True, methods=["get"], url_path="report")
    def report(self, request, pk=None):
        """Reconciliation report: summary, every statement line with what it cleared against. ?fmt=csv|pdf"""
        st = self.get_object()
        s = services.summary(st)
        rows = []
        for l in st.lines.prefetch_related("matches__journal_line__transaction").order_by("date", "row"):
            eff = [m for m in l.matches.all() if m.journal_line.transaction.status == "posted"]
            matched = sum((m.amount for m in eff), 0)
            rows.append({"date": l.date, "description": l.description, "reference": l.reference, "amount": l.amount,
                         "status": services.line_status(l, matched), "matched_to": ", ".join(m.journal_line.transaction.reference for m in eff)})
        head = [("Account", st.account.name), ("Statement", st.file_name), ("Period", f"{st.period_start} to {st.period_end}"),
                ("Status", st.status), ("Opening balance", st.opening_balance), ("Closing balance", st.closing_balance),
                ("Book balance at period end", s["book_balance_at_period_end"]), ("Matched net", s["matched_net"]), ("Difference", s["difference"]),
                ("Uncleared book items", s["uncleared_book_items"]), ("Uncleared book net", s["uncleared_book_net"])]
        fmt = request.query_params.get("fmt", "csv")
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            for k, v in head:
                w.writerow([_csv_safe(k), _csv_safe(v)])
            w.writerow([])
            w.writerow(["Date", "Description", "Reference", "Amount", "Status", "Matched to"])
            for r in rows:
                w.writerow([r["date"], _csv_safe(r["description"]), _csv_safe(r["reference"]), r["amount"], r["status"], _csv_safe(r["matched_to"])])
            resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="reconciliation-{st.pk}.csv"'
            return resp
        if fmt == "pdf":
            from apps.documents.pdf import reconciliation_pdf

            data = reconciliation_pdf({"org_name": request.organization.name, "title": f"Bank reconciliation: {st.account.name}", "head": head, "rows": rows})
            resp = HttpResponse(data, content_type="application/pdf")
            resp["Content-Disposition"] = f'inline; filename="reconciliation-{st.pk}.pdf"'
            return resp
        raise DomainError("fmt must be csv or pdf.", "invalid_format")
