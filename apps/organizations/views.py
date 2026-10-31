from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import ADMIN_ROLES
from apps.core.views import OrgScopedMixin

from .models import Branch, Membership, Organization
from .serializers import BranchSerializer, MembershipSerializer, OrganizationSerializer
from .services import create_organization


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

    def perform_update(self, serializer):
        m = Membership.objects.get(user=self.request.user, organization=serializer.instance)
        if m.role not in ADMIN_ROLES:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("Only owners/admins can edit the organization.")
        serializer.save()


class BranchViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    write_roles = ADMIN_ROLES
    queryset = Branch.objects.all().order_by("name")
    serializer_class = BranchSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]  # branches may be deactivated, not deleted


class MembershipViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Membership.objects.select_related("user").order_by("id")
    serializer_class = MembershipSerializer
