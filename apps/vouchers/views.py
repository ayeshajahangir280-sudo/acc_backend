from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgCreateReadViewSet

from . import services
from .models import Voucher
from .serializers import VoucherListSerializer, VoucherSerializer


class VoucherViewSet(OrgCreateReadViewSet):
    """General / Journal / Contra vouchers. Filter registers with ?type=general|journal|contra."""

    queryset = Voucher.objects.order_by("-date", "-id")
    filterset_fields = {"date": ["gte", "lte"], "type": ["exact"], "status": ["exact"]}
    search_fields = ["number", "narration"]
    ordering_fields = ["date", "number", "total"]

    def get_serializer_class(self):
        return VoucherListSerializer if self.action == "list" else VoucherSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.prefetch_related("lines") if self.action != "list" else qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        v, created = services.create_voucher(
            request.organization, request.user, d["type"], date=d["date"], lines=d["lines"],
            narration=d.get("narration", ""), branch=d.get("branch"), idempotency_key=d.get("idempotency_key"),
        )
        return Response(VoucherSerializer(v, context=self.get_serializer_context()).data, status=201 if created else 200)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        return Response(VoucherListSerializer(services.void_voucher(self.get_object(), request.user)).data)
