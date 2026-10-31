from rest_framework import viewsets

from apps.core.views import OrgScopedMixin

from .models import Party
from .serializers import PartySerializer


class PartyViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    """Parties with history cannot be hard-deleted (ledger rows protect them); deactivate instead."""

    queryset = Party.objects.all()
    serializer_class = PartySerializer
    filterset_fields = ["kind", "is_active"]
    search_fields = ["name", "email", "phone", "tax_number"]
    ordering_fields = ["name", "created_at"]
    ordering = ["name"]
    http_method_names = ["get", "post", "patch", "head", "options"]
