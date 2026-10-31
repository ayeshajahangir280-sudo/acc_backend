from rest_framework import viewsets

from apps.core.views import OrgScopedMixin

from .models import TaxRate
from .serializers import TaxRateSerializer


class TaxRateViewSet(OrgScopedMixin, viewsets.ModelViewSet):
    """Historic documents keep the rate they were posted with, so rates are deactivated rather than deleted."""

    queryset = TaxRate.objects.all()
    serializer_class = TaxRateSerializer
    filterset_fields = ["is_active"]
    ordering = ["rate", "name"]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]
