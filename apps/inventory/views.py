from django.db.models import Sum
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgCreateReadViewSet, OrgReadOnlyViewSet, OrgScopedMixin

from . import services
from .models import Item, Location, ProductionOrder, StockMovement
from .serializers import (AdjustmentSerializer, ItemSerializer, LocationSerializer, MovementSerializer,
                          ProductionListSerializer, ProductionSerializer)


class ItemViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    filterset_fields = ["kind", "is_active"]
    search_fields = ["sku", "name"]
    ordering = ["name"]
    http_method_names = ["get", "post", "patch", "head", "options"]  # items with history are deactivated, not deleted


class LocationViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    filterset_fields = ["kind", "is_active"]
    ordering = ["name"]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]

    def list(self, request, *args, **kwargs):
        if request.method == "GET":
            services.default_location(request.organization)  # every org always has a default warehouse
        return super().list(request, *args, **kwargs)


class StockViewSet(OrgScopedMixin, viewsets.GenericViewSet):
    """On-hand quantities per item and location, computed from the movement ledger."""

    queryset = StockMovement.objects.all()

    def list(self, request):
        qs = self.get_queryset()
        p = request.query_params
        if p.get("item"):
            qs = qs.filter(item_id=p["item"])
        if p.get("location"):
            qs = qs.filter(location_id=p["location"])
        if p.get("kind"):
            qs = qs.filter(item__kind=p["kind"])
        if p.get("search"):
            qs = qs.filter(item__name__icontains=p["search"]) | qs.filter(item__sku__icontains=p["search"])
        rows = (qs.values("item_id", "item__sku", "item__name", "item__kind", "item__unit", "location_id", "location__name")
                .annotate(quantity=Sum("quantity")).exclude(quantity=0).order_by("item__name", "location__name"))
        page = self.paginate_queryset(rows)
        data = [{"item": r["item_id"], "sku": r["item__sku"], "item_name": r["item__name"], "kind": r["item__kind"],
                 "unit": r["item__unit"], "location": r["location_id"], "location_name": r["location__name"],
                 "quantity": r["quantity"]} for r in page]
        return self.get_paginated_response(data)


class MovementViewSet(OrgReadOnlyViewSet):
    queryset = StockMovement.objects.select_related("item", "location").order_by("-date", "-id")
    serializer_class = MovementSerializer
    filterset_fields = {"item": ["exact"], "location": ["exact"], "type": ["exact"], "date": ["gte", "lte"]}
    search_fields = ["item__sku", "item__name", "note"]


class AdjustmentViewSet(OrgScopedMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """Opening balances and manual corrections. Recorded as movements; cannot make stock negative."""

    queryset = StockMovement.objects.none()
    serializer_class = AdjustmentSerializer

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        m = services.adjust_stock(request.organization, request.user, item=d["item"],
                                  location=d.get("location") or services.default_location(request.organization),
                                  quantity=d["quantity"], date=d["date"], mtype=d["type"], note=d["note"])
        return Response(MovementSerializer(m).data, status=201)


class ProductionViewSet(OrgCreateReadViewSet):
    queryset = ProductionOrder.objects.select_related("item", "location").order_by("-date", "-id")
    filterset_fields = {"date": ["gte", "lte"], "item": ["exact"], "status": ["exact"]}
    search_fields = ["number", "item__name", "item__sku", "note"]

    def get_serializer_class(self):
        return ProductionListSerializer if self.action == "list" else ProductionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.prefetch_related("inputs__item") if self.action != "list" else qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        order, created = services.create_production(
            request.organization, request.user, date=d["date"], item=d["item"], quantity=d["quantity"],
            location=d.get("location") or services.default_location(request.organization), inputs=d["inputs"],
            note=d.get("note", ""), idempotency_key=d.get("idempotency_key"))
        return Response(ProductionSerializer(order, context=self.get_serializer_context()).data, status=201 if created else 200)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        order = services.void_production(self.get_object(), request.user)
        return Response(ProductionListSerializer(order).data)
