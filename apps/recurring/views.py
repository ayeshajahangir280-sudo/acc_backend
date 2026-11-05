from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.views import OrgScopedMixin

from . import services
from .models import RecurringDocument, RecurringExpense
from .serializers import RecurringExpenseSerializer, RecurringSerializer


class RecurringViewSet(OrgScopedMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Recurring invoice/bill profiles. Filter with ?side=sales|purchases. Never deleted: pause instead."""

    branch_scoped = True
    queryset = RecurringDocument.objects.select_related("party").prefetch_related("items").order_by("name", "id")
    serializer_class = RecurringSerializer
    filterset_fields = {"side": ["exact"], "is_active": ["exact"]}
    search_fields = ["name", "party__name"]

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        p = services.create_profile(request.organization, request.user, items=d.pop("items"), party=d.pop("party"),
                                    branch=d.pop("branch", None), **d)
        return Response(self.get_serializer(p).data, status=201)

    def update(self, request, *args, **kwargs):
        p = self.get_object()
        ser = self.get_serializer(p, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        for locked in ("side", "party", "branch"):
            if locked in d and (d[locked] != getattr(p, locked)):
                raise serializers.ValidationError({locked: "Cannot be changed; create a new profile."})
            d.pop(locked, None)
        p = services.update_profile(request.organization, p, items=d.pop("items", None), **d)
        return Response(self.get_serializer(self.get_queryset().get(pk=p.pk)).data)

    partial_update = update

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        return Response(self.get_serializer(services.set_active(self.get_object(), False)).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        return Response(self.get_serializer(services.set_active(self.get_object(), True)).data)

    @action(detail=True, methods=["post"], url_path="run-now")
    def run_now(self, request, pk=None):
        """Create any occurrences that are due today or earlier."""
        p = self.get_object()
        made = services.run_profile(p.pk)
        p.refresh_from_db()
        return Response({"created": [{"id": d.id, "number": d.number, "date": d.date} for d in made],
                         "next_date": (services.next_date(p) or None) and services.next_date(p).isoformat(), "last_error": p.last_error})


class RecurringExpenseViewSet(OrgScopedMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Recurring expense profiles. Never deleted: pause instead."""

    branch_scoped = True
    queryset = RecurringExpense.objects.select_related("expense_account", "paid_through", "vendor").order_by("name", "id")
    serializer_class = RecurringExpenseSerializer
    filterset_fields = {"is_active": ["exact"]}
    search_fields = ["name", "vendor__name", "expense_account__name"]

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        p = services.create_expense_profile(request.organization, request.user, **ser.validated_data)
        return Response(self.get_serializer(p).data, status=201)

    def update(self, request, *args, **kwargs):
        p = self.get_object()
        ser = self.get_serializer(p, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        if "branch" in d and d["branch"] != p.branch:
            raise serializers.ValidationError({"branch": "Cannot be changed; create a new profile."})
        d.pop("branch", None)
        p = services.update_expense_profile(request.organization, request.user, p, **d)
        return Response(self.get_serializer(self.get_queryset().get(pk=p.pk)).data)

    partial_update = update

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        return Response(self.get_serializer(services.set_expense_active(self.get_object(), False)).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        return Response(self.get_serializer(services.set_expense_active(self.get_object(), True)).data)

    @action(detail=True, methods=["post"], url_path="run-now")
    def run_now(self, request, pk=None):
        p = self.get_object()
        made = services.run_expense_profile(p.pk)
        p.refresh_from_db()
        nd = services.next_date(p)
        return Response({"created": [{"id": e.id, "number": e.number, "date": e.date} for e in made],
                         "next_date": nd.isoformat() if nd else None, "last_error": p.last_error})
