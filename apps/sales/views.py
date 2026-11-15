from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.errors import DomainError
from apps.core.views import OrgCreateReadViewSet
from apps.documents.views import DocumentPdfMixin
from apps.treasury.credit_views import CreditActionsMixin

from . import services
from .models import SalesInvoice
from .serializers import SalesInvoiceListSerializer, SalesInvoiceSerializer


class SalesInvoiceViewSet(DocumentPdfMixin, CreditActionsMixin, OrgCreateReadViewSet):
    """Sales register (list/filter) + create + void. Posted documents are never edited or deleted."""

    branch_scoped = True
    doc_side = "sales"
    credit_direction = "receipt"
    queryset = SalesInvoice.objects.select_related("party").order_by("-date", "-id")
    filterset_fields = {"date": ["gte", "lte"], "party": ["exact"], "kind": ["exact"], "status": ["exact"]}
    search_fields = ["number", "party__name", "narration"]
    ordering_fields = ["date", "number", "total"]

    def get_serializer_class(self):
        return SalesInvoiceListSerializer if self.action == "list" else SalesInvoiceSerializer

    PAYMENT_STATUSES = ("paid", "settled", "pending", "overdue", "open")

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs.prefetch_related("items")
        return self._filter_payment_status(qs)

    def _filter_payment_status(self, qs):
        """Optional read-only `?payment_status=paid|settled|pending|overdue|open` list filter.

        Derived from the same outstanding balance the receipts screen uses (allocations, returns, credits, refunds);
        nothing is stored or changed. Only posted invoices qualify, so a voided invoice is never "paid".
          - paid:    nothing outstanding and money or customer credit was actually applied to it
          - settled: nothing outstanding but no payment or credit applied, i.e. closed by sales returns
          - pending / overdue: still has a balance (including partially paid), split by due date (or invoice date)
        """
        status = self.request.query_params.get("payment_status")
        if not status:
            return qs
        if status not in self.PAYMENT_STATUSES:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"payment_status": "Use paid, settled, pending, overdue or open."})
        from django.db.models import Q
        from django.db.models.functions import Coalesce
        from django.utils import timezone

        from apps.treasury.selectors import outstanding_invoices

        inv = outstanding_invoices(self.request.organization, "receipt")
        received = Q(allocated__gt=0) | Q(applied_in__gt=0)
        if status == "paid":
            ids = inv.filter(Q(outstanding__lte=0) & received)
        elif status == "settled":
            ids = inv.filter(Q(outstanding__lte=0) & ~received)
        else:
            ids = inv.filter(outstanding__gt=0)
            if status != "open":
                ids = ids.annotate(eff_due=Coalesce("due_date", "date"))
                today = timezone.localdate()
                ids = ids.filter(eff_due__gte=today) if status == "pending" else ids.filter(eff_due__lt=today)
        return qs.filter(pk__in=ids.values("pk"))

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        billable = self._billable(request, d.get("billable_expenses"))
        doc, created = services.create_document(
            request.organization, request.user, kind=d.get("kind", SalesInvoice.Kind.INVOICE), party=d["party"],
            date=d["date"], items=d["items"], branch=d.get("branch"), narration=d.get("narration", ""),
            original=d.get("original"), due_date=d.get("due_date"), idempotency_key=d.get("idempotency_key"),
            billable_expenses=billable,
        )
        return Response(SalesInvoiceSerializer(doc, context=self.get_serializer_context()).data, status=201 if created else 200)

    @staticmethod
    def _billable(request, ids):
        """Only expenses the caller can see (organization and branch scope); anything else is simply not found."""
        if not ids:
            return None
        from apps.expenses.models import Expense
        from apps.organizations.access import scope_queryset

        found = list(scope_queryset(Expense.objects.filter(organization=request.organization, pk__in=ids), request))
        if len(found) != len(set(ids)):
            raise DomainError("Expense not found.", "invalid_billable")
        return found

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        doc = services.void_document(self.get_object(), request.user)
        return Response(SalesInvoiceListSerializer(doc).data)
