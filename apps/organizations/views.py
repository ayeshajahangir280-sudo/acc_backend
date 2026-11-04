import datetime

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import ADMIN_ROLES
from apps.core.views import OrgScopedMixin

from .access import scope_for_membership
from .models import Branch, BranchAccess, Membership, Organization
from .serializers import (BranchAccessSerializer, BranchSerializer, FiscalYearCloseSerializer, MembershipSerializer,
                          OrganizationSerializer)
from .services import close_fiscal_year, create_organization, fiscal_year_status


class OrganizationViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                          mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Organizations the current user belongs to. Not scoped by header (it is how you pick one)."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrganizationSerializer
    filter_backends = []

    def get_queryset(self):
        return Organization.objects.filter(memberships__user=self.request.user, is_active=True).prefetch_related(
            "memberships"
        ).order_by("name")

    def perform_create(self, serializer):
        serializer.instance = create_organization(self.request.user, **serializer.validated_data)

    def update(self, request, *args, **kwargs):
        # Authorise before validating, so a non-admin gets 403 (not field errors) for any edit, including the period lock.
        org = self.get_object()
        m = Membership.objects.get(user=request.user, organization=org)
        if m.role not in ADMIN_ROLES:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only owners/admins can edit the organization.")
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        m = Membership.objects.get(user=self.request.user, organization=serializer.instance)
        if m.role not in ADMIN_ROLES:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only owners/admins can edit the organization.")
        serializer.save()


    @action(detail=True, methods=["get"], url_path="fiscal-years")
    def fiscal_years(self, request, pk=None):
        """Next fiscal year that can be closed, and the closes already recorded."""
        org = self.get_object()
        nxt, closes = fiscal_year_status(org)
        return Response({"fiscal_year_start_month": org.fiscal_year_start_month, "next_year_end": nxt,
                         "closes": FiscalYearCloseSerializer(closes, many=True).data})

    @action(detail=True, methods=["post"], url_path="close-year")
    def close_year(self, request, pk=None):
        org = self.get_object()
        if Membership.objects.get(user=request.user, organization=org).role not in ADMIN_ROLES:
            raise PermissionDenied("Only owners/admins can close a fiscal year.")
        try:
            year_end = datetime.date.fromisoformat(str(request.data.get("year_end")))
        except ValueError:
            raise ValidationError({"year_end": "Send a date as YYYY-MM-DD."})
        return Response(FiscalYearCloseSerializer(close_fiscal_year(org, request.user, year_end)).data, status=201)


class BranchViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    write_roles = ADMIN_ROLES
    queryset = Branch.objects.all().order_by("name")
    serializer_class = BranchSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]  # branches may be deactivated, not deleted

    def get_queryset(self):
        qs = super().get_queryset()
        scope = scope_for_membership(self.request.membership)
        return qs if scope is None else qs.filter(pk__in=scope)  # restricted users only see/pick their own branches


class MembershipViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Membership.objects.select_related("user").order_by("id")
    serializer_class = MembershipSerializer


class BranchAccessViewSet(OrgScopedMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """Who may use which branch. Owners/admins manage it; a restricted member can only read their own rows."""

    write_roles = ADMIN_ROLES
    queryset = BranchAccess.objects.select_related("membership__user", "branch").order_by("membership_id", "branch__name")
    serializer_class = BranchAccessSerializer
    filterset_fields = ["membership", "branch"]

    def get_queryset(self):
        qs = self.queryset.filter(membership__organization=self.request.organization)
        if self.request.membership.role not in ADMIN_ROLES:
            qs = qs.filter(membership=self.request.membership)
        return qs
