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

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.prefetch_related("items") if self.action != "list" else qs

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
