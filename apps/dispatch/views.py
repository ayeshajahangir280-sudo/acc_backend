from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgCreateReadViewSet, OrgScopedMixin

from . import services
from .models import VanLoad, Vehicle
from .serializers import CloseSerializer, LoadListSerializer, LoadSerializer, VehicleSerializer


class VehicleViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = Vehicle.objects.all()
    serializer_class = VehicleSerializer
    filterset_fields = ["is_active"]
    search_fields = ["name", "plate_number", "driver_name"]
    ordering = ["name"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def perform_create(self, serializer):
        serializer.instance = services.create_vehicle(self.request.organization, **serializer.validated_data)


class VanLoadViewSet(OrgCreateReadViewSet):
    queryset = VanLoad.objects.select_related("vehicle").order_by("-dispatch_at", "-id")
    filterset_fields = {"vehicle": ["exact"], "status": ["exact"], "dispatch_at": ["gte", "lte"]}
    search_fields = ["number", "vehicle__name", "assigned_to"]

    def get_serializer_class(self):
        return LoadListSerializer if self.action == "list" else LoadSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.prefetch_related("lines__item") if self.action != "list" else qs

    def _out(self, load):
        load = self.get_queryset().get(pk=load.pk)
        return Response(LoadSerializer(load, context=self.get_serializer_context()).data)

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        load, created = services.create_load(
            request.organization, request.user, vehicle=d["vehicle"], dispatch_at=d["dispatch_at"],
            lines=[{"item": l["item"], "quantity": l["quantity"]} for l in d["lines"]], source_location=d.get("source_location"),
            assigned_to=d.get("assigned_to", ""), note=d.get("note", ""), idempotency_key=d.get("idempotency_key"))
        resp = self._out(load)
        resp.status_code = 201 if created else 200
        return resp

    @action(detail=True, methods=["post"], url_path="dispatch")
    def send_out(self, request, pk=None):  # not named `dispatch`: that would shadow DRF's request dispatcher
        return self._out(services.dispatch(self.get_object()))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._out(services.cancel(self.get_object(), request.user))

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        ser = CloseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        for l in ser.validated_data["lines"]:
            if l["item"].organization_id != request.organization.id:
                raise serializers.ValidationError({"lines": "Item not found."})
        return self._out(services.close(self.get_object(), request.user, ser.validated_data["lines"]))
