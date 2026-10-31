from django.db import models
from django.db.models import Q

from apps.core.models import OrgOwned


class TaxRate(OrgOwned):
    """An organization-defined tax rate (e.g. a VAT rate). No rates are preloaded: the organization enters its own."""

    name = models.CharField(max_length=60)
    rate = models.DecimalField(max_digits=5, decimal_places=2)  # percent
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_taxrate_name"),
            models.CheckConstraint(condition=Q(rate__gte=0, rate__lte=100), name="taxrate_range"),
        ]
