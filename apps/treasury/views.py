from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgCreateReadViewSet

from . import services
from .models import Payment, Receipt
from .selectors import outstanding_invoices
from .serializers import PaymentSerializer, ReceiptSerializer


class _OpenInvoiceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField()
    date = serializers.DateField()
    due_date = serializers.DateField()
    total = serializers.DecimalField(max_digits=18, decimal_places=2)
    allocated = serializers.DecimalField(max_digits=18, decimal_places=2)
    returned = serializers.DecimalField(max_digits=18, decimal_places=2)
    applied_in = serializers.DecimalField(max_digits=18, decimal_places=2)
    applied_out = serializers.DecimalField(max_digits=18, decimal_places=2)
    outstanding = serializers.DecimalField(max_digits=18, decimal_places=2)
    branch = serializers.IntegerField(source="branch_id", allow_null=True)


class _CashViewSet(OrgCreateReadViewSet):
    direction = None
    branch_scoped = True
    filterset_fields = {"date": ["gte", "lte"], "party": ["exact"], "method": ["exact"], "status": ["exact"]}
    search_fields = ["number", "reference", "party__name", "narration"]
    ordering_fields = ["date", "number", "amount"]

    def get_queryset(self):
        return super().get_queryset().prefetch_related("allocations__invoice")

    def _check_invoices(self, allocations):
        from apps.organizations.access import branch_visible

        for a in allocations:
            if a["invoice"].organization_id != self.request.organization.id or not branch_visible(self.request, a["invoice"].branch_id):
                raise serializers.ValidationError({"allocations": "Invoice not found."})

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        allocations = data.pop("allocations", [])
        doc, created = services.create_cash_document(
            request.organization, request.user, self.direction, allocations=allocations, **data)
        return Response(self.get_serializer(doc).data, status=201 if created else 200)

    @action(detail=True, methods=["post"])
    def allocate(self, request, pk=None):
        """Apply (more of) the unapplied amount to invoices."""
        field = self.get_serializer().fields["allocations"]
        allocations = field.run_validation(request.data.get("allocations", []))
        self._check_invoices(allocations)
        doc = services.allocate_existing(request.organization, self.get_object(), self.direction, allocations)
        return Response(self.get_serializer(self.get_queryset().get(pk=doc.pk)).data)

    @action(detail=False, methods=["get"], url_path="open-invoices")
    def open_invoices(self, request):
        """Invoices of one party that still have an outstanding balance (oldest due first)."""
        from apps.organizations.access import request_scope

        qs = outstanding_invoices(request.organization, self.direction, party=request.query_params.get("party"),
                                  as_of=request.query_params.get("as_of"), branch=request_scope(request))
        return Response(_OpenInvoiceSerializer(qs.filter(outstanding__gt=0).order_by("due_date", "id"), many=True).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        doc = services.void_cash_document(self.get_object(), request.user)
        return Response(self.get_serializer(doc).data)


class ReceiptViewSet(_CashViewSet):
    direction = "receipt"
    queryset = Receipt.objects.select_related("party").order_by("-date", "-id")
    serializer_class = ReceiptSerializer


class PaymentViewSet(_CashViewSet):
    direction = "payment"
    queryset = Payment.objects.select_related("party").order_by("-date", "-id")
    serializer_class = PaymentSerializer
