from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgCreateReadViewSet

from . import services
from .models import PurchaseOrder, SalesOrder
from .serializers import ConvertLineSerializer, PurchaseOrderSerializer, SalesOrderSerializer


class _OrderViewSet(OrgCreateReadViewSet):
    side = None
    branch_scoped = True
    filterset_fields = {"date": ["gte", "lte"], "party": ["exact"], "status": ["exact"]}
    search_fields = ["number", "party__name", "narration"]
    ordering_fields = ["date", "number", "total"]

    def get_queryset(self):
        return super().get_queryset().select_related("party", "invoice").prefetch_related("items", "conversions__invoice")

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        order, created = services.create_order(
            request.organization, request.user, self.side, kind=d.get("kind", "purchase"), party=d["party"], date=d["date"],
            items=d["items"], branch=d.get("branch"), valid_until=d.get("valid_until"), narration=d.get("narration", ""),
            idempotency_key=d.get("idempotency_key"))
        return Response(self.get_serializer(order).data, status=201 if created else 200)

    def update(self, request, *args, **kwargs):
        order = self.get_object()
        ser = self.get_serializer(order, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if "items" not in d:
            raise serializers.ValidationError({"items": "Required."})
        order = services.update_order(request.organization, self.side, order, items=d["items"], date=d.get("date"),
                                      valid_until=d.get("valid_until"), narration=d.get("narration"))
        return Response(self.get_serializer(self.get_queryset().get(pk=order.pk)).data)

    partial_update = update

    def _status(self, new):
        order = services.set_status(self.side, self.get_object(), new)
        return Response(self.get_serializer(self.get_queryset().get(pk=order.pk)).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._status("cancelled")

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return self._status("accepted")

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        return self._status("declined")

    @action(detail=True, methods=["post"])
    def convert(self, request, pk=None):
        order = self.get_object()
        to = request.data.get("to", "invoice")
        date, due = request.data.get("date"), request.data.get("due_date")
        f = serializers.DateField(required=False, allow_null=True)
        lines = request.data.get("lines")
        if lines is not None:
            lines = ConvertLineSerializer(data=lines, many=True)
            lines.is_valid(raise_exception=True)
            lines = [dict(x) for x in lines.validated_data]
        res = services.convert(request.organization, request.user, self.side, order, to=to,
                               date=f.run_validation(date) if date else None, due_date=f.run_validation(due) if due else None,
                               lines=lines, idempotency_key=request.data.get("idempotency_key") or None)
        if to == "order":
            return Response(SalesOrderSerializer(res, context=self.get_serializer_context()).data, status=201)
        return Response({"invoice": res.id, "number": res.number, "order": order.id}, status=201)


class SalesOrderViewSet(_OrderViewSet):
    side = "sales"
    queryset = SalesOrder.objects.order_by("-date", "-id")
    serializer_class = SalesOrderSerializer
    filterset_fields = {**_OrderViewSet.filterset_fields, "kind": ["exact"]}


class PurchaseOrderViewSet(_OrderViewSet):
    side = "purchases"
    queryset = PurchaseOrder.objects.order_by("-date", "-id")
    serializer_class = PurchaseOrderSerializer
