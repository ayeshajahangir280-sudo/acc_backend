import datetime

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import perms
from apps.core.permissions import ADMIN_ROLES
from apps.core.views import OrgScopedMixin

from .access import scope_for_membership
from .models import Branch, BranchAccess, Membership, Organization
from .serializers import (BranchAccessSerializer, BranchSerializer, FiscalYearCloseSerializer, MembershipSerializer,
                          OrganizationSerializer)
from .models import PeriodLockChange
from .services import change_lock_manually, close_fiscal_year, create_organization, fiscal_year_status, reopen_fiscal_year


def _require(user, org, module, action):
    if not perms.has(_member(user, org), module, action):
        from rest_framework.exceptions import PermissionDenied

        if action == "view":
            raise PermissionDenied("You do not have permission to view organization settings.")
        raise PermissionDenied("You do not have permission to change organization settings.")


def _member(user, org):
    """The caller's active membership, or 403. (Deactivated members cannot act on the organization.)"""
    from rest_framework.exceptions import PermissionDenied

    from .members import active_membership

    m = active_membership(user, org.pk)
    if m is None:
        raise PermissionDenied("You do not have access to this organization.")
    return m


class OrganizationViewSet(mixins.CreateModelMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin,
                          mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Organizations the current user belongs to. Not scoped by header (it is how you pick one)."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrganizationSerializer
    filter_backends = []

    def get_queryset(self):
        # Only organizations where the user's membership is active (a deactivated member no longer sees it at all).
        return Organization.objects.filter(memberships__user=self.request.user, memberships__is_active=True, is_active=True).prefetch_related(
            "memberships"
        ).order_by("name")

    def perform_create(self, serializer):
        serializer.instance = create_organization(self.request.user, **serializer.validated_data)

    def retrieve(self, request, *args, **kwargs):
        org = self.get_object()
        _require(request.user, org, "settings", "view")
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        # Authorise before validating, so a non-admin gets 403 (not field errors) for any edit, including the period lock.
        org = self.get_object()
        _require(request.user, org, "settings", "manage")
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        _require(self.request.user, serializer.instance, "settings", "manage")
        from django.db import transaction

        with transaction.atomic():
            locked = Organization.objects.select_for_update().get(pk=serializer.instance.pk)  # serialises lock changes
            serializer.instance.refresh_from_db()
            old = locked.books_locked_through
            new = serializer.validated_data.get("books_locked_through", old)
            if new != old:
                change_lock_manually(locked, self.request.user, old, new, self.request.data.get("lock_change_reason"))
            serializer.save()


    @action(detail=True, methods=["get"], url_path="fiscal-years")
    def fiscal_years(self, request, pk=None):
        """Next fiscal year that can be closed, and the closes already recorded."""
        org = self.get_object()
        _require(request.user, org, "settings", "view")
        nxt, closes = fiscal_year_status(org)
        return Response({"fiscal_year_start_month": org.fiscal_year_start_month, "next_year_end": nxt,
                         "closes": FiscalYearCloseSerializer(closes, many=True).data})

    @action(detail=True, methods=["post"], url_path="close-year")
    def close_year(self, request, pk=None):
        org = self.get_object()
        if not perms.has(_member(request.user, org), "settings", "manage"):
            raise PermissionDenied("Only owners/admins can close a fiscal year.")
        try:
            year_end = datetime.date.fromisoformat(str(request.data.get("year_end")))
        except ValueError:
            raise ValidationError({"year_end": "Send a date as YYYY-MM-DD."})
        return Response(FiscalYearCloseSerializer(close_fiscal_year(org, request.user, year_end)).data, status=201)


    @action(detail=True, methods=["post"], url_path="reopen-year")
    def reopen_year(self, request, pk=None):
        org = self.get_object()
        if _member(request.user, org).role != "owner":
            raise PermissionDenied("Only the owner can reopen a fiscal year.")
        try:
            year_end = datetime.date.fromisoformat(str(request.data.get("year_end")))
        except ValueError:
            raise ValidationError({"year_end": "Send a date as YYYY-MM-DD."})
        return Response(FiscalYearCloseSerializer(reopen_fiscal_year(org, request.user, year_end, request.data.get("reason"))).data)

    @action(detail=True, methods=["get"], url_path="lock-history")
    def lock_history(self, request, pk=None):
        """Who changed the period lock, when, from what to what, and why. Owners and admins only."""
        org = self.get_object()
        if not perms.has(_member(request.user, org), "settings", "manage"):
            raise PermissionDenied("Only owners/admins can view the lock history.")
        rows = PeriodLockChange.objects.filter(organization=org).select_related("changed_by")[:200]
        return Response([{"id": r.id, "at": r.created_at, "old_value": r.old_value, "new_value": r.new_value, "source": r.source,
                          "reason": r.reason, "by": r.changed_by.username if r.changed_by else None} for r in rows])


class BranchViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    write_roles = ADMIN_ROLES
    queryset = Branch.objects.all().order_by("name")
    serializer_class = BranchSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]  # branches may be deactivated, not deleted

    def get_queryset(self):
        qs = super().get_queryset()
        scope = scope_for_membership(self.request.membership)
        return qs if scope is None else qs.filter(pk__in=scope)  # restricted users only see/pick their own branches


class MembershipViewSet(OrgScopedMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.ReadOnlyModelViewSet):
    """Members of the current organization. Any member may list them; owners/admins change role / status or remove.
    The rules (self-change, owner-only, last owner) are enforced in apps.organizations.members."""

    queryset = Membership.objects.select_related("user", "invited_by").order_by("id")
    serializer_class = MembershipSerializer
    write_roles = ADMIN_ROLES
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def partial_update(self, request, *args, **kwargs):
        from . import members

        target = self.get_object()
        role = request.data.get("role")
        active = request.data.get("is_active")
        if active is not None and not isinstance(active, bool):
            raise ValidationError({"is_active": "Must be true or false."})
        kw = {"permissions": request.data.get("permissions")} if "permissions" in request.data else {}
        m = members.update_member(request.organization, request.membership, target, role=role, is_active=active, **kw)
        return Response(self.get_serializer(m).data)

    def destroy(self, request, *args, **kwargs):
        from . import members

        members.remove_member(request.organization, request.membership, self.get_object())
        return Response(status=204)


class InvitationViewSet(OrgScopedMixin, mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Owners/admins invite people by email. An existing account is added directly; otherwise an invitation link is
    created (emailed when mail is configured, and always returned so it can be copied)."""

    from .models import Invitation as _Inv

    queryset = _Inv.objects.select_related("invited_by").order_by("-id")
    serializer_class = None
    write_roles = ADMIN_ROLES
    pagination_class = None

    def get_serializer_class(self):
        from .serializers import InvitationSerializer

        return InvitationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if self.request.query_params.get("all") else qs.filter(accepted_at__isnull=True, cancelled_at__isnull=True)

    def _link_response(self, inv, raw, status_code=200):
        from . import members

        url, path = members.invite_link(self.request, raw)
        emailed = members.send_invite_email(self.request.organization, inv, url)
        return Response({"kind": "invitation", "invitation": self.get_serializer(inv).data, "invite_url": url, "invite_path": path,
                         "emailed": emailed}, status=status_code)

    def create(self, request, *args, **kwargs):
        from . import members

        kind, obj, raw = members.invite(request.organization, request.membership, email=request.data.get("email"),
                                        role=request.data.get("role") or Membership.Role.ACCOUNTANT,
                                        permissions=request.data.get("permissions"))
        if kind == "member":
            return Response({"kind": "member", "member": MembershipSerializer(obj, context={"request": request}).data}, status=201)
        return self._link_response(obj, raw, 201)

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        from . import members

        inv, raw = members.resend(self.get_object(), request.membership)
        return self._link_response(inv, raw)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from . import members

        return Response(self.get_serializer(members.cancel(self.get_object(), request.membership)).data)


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


class PermissionCatalogView(APIView):
    """Modules, actions and role presets, for building permission screens."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "actions": perms.ACTIONS,
            "modules": [{"key": k, "label": label, "group": g, "actions": acts} for k, label, g, acts in perms.MODULES],
            "presets": {role: perms.as_lists(p) for role, p in perms.PRESETS.items()},
        })
