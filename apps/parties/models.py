from django.db import models

from apps.core.models import OrgOwned


class Party(OrgOwned):
    class Kind(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        SUPPLIER = "supplier", "Supplier"
        BOTH = "both", "Customer & Supplier"

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    tax_number = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "parties"
        constraints = [models.UniqueConstraint(fields=["organization", "name", "kind"], name="uniq_party_name_kind")]
        indexes = [models.Index(fields=["organization", "kind", "name"])]

    def __str__(self):
        return self.name
