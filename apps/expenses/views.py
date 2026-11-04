import os

from django.http import FileResponse, Http404
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.errors import DomainError
from apps.core.views import OrgCreateReadViewSet

from . import services
from .models import Expense
from .serializers import ExpenseListSerializer, ExpenseSerializer


class ExpenseViewSet(OrgCreateReadViewSet):
    """Expenses: create / list / retrieve / void, plus a receipt attachment. Filter with ?expense_account=&vendor=&branch=&status=."""

    branch_scoped = True
    queryset = Expense.objects.select_related("expense_account", "paid_through", "vendor").order_by("-date", "-id")
    filterset_fields = {"date": ["gte", "lte"], "expense_account": ["exact"], "vendor": ["exact"], "branch": ["exact"], "status": ["exact"]}
    search_fields = ["number", "reference", "narration"]
    ordering_fields = ["date", "number", "total"]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_serializer_class(self):
        return ExpenseListSerializer if self.action == "list" else ExpenseSerializer

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        exp, created = services.create_expense(
            request.organization, request.user, date=d["date"], expense_account=d["expense_account"], paid_through=d["paid_through"],
            amount=d["amount"], tax_rate=d.get("tax_rate") or 0, amount_includes_tax=d.get("amount_includes_tax", False),
            vendor=d.get("vendor"), reference=d.get("reference", ""), narration=d.get("narration", ""), branch=d.get("branch"),
            idempotency_key=d.get("idempotency_key"),
        )
        return Response(ExpenseListSerializer(exp).data, status=201 if created else 200)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        return Response(ExpenseListSerializer(services.void_expense(self.get_object(), request.user)).data)

    @action(detail=True, methods=["post", "get"], url_path="receipt")
    def receipt(self, request, pk=None):
        exp = self.get_object()  # branch/org scoped: out-of-scope expenses are 404 here too
        if request.method == "POST":
            upload = request.FILES.get("file")
            if upload is None:
                raise DomainError("Send the receipt as multipart field 'file'.", "receipt_missing")
            return Response(ExpenseListSerializer(services.attach_receipt(exp, upload)).data)
        if not exp.receipt:
            raise Http404
        resp = FileResponse(exp.receipt.open("rb"), as_attachment=True, filename=os.path.basename(exp.receipt.name))
        resp["X-Content-Type-Options"] = "nosniff"
        return resp
