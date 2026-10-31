from django.conf import settings
from django.db import models

from apps.core.models import TimeStamped


class Organization(TimeStamped):
    """Tenant boundary. All accounting data hangs off exactly one organization.

    Assumption (see docs/ASSUMPTIONS.md): "company" is not a separate tier yet; an organization
    may have optional branches. A legal-entity tier can be inserted later without touching ledger tables.
    """

    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    base_currency = models.CharField(max_length=3, default="USD")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Branch(TimeStamped):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="branches")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_branch_name_per_org")]

    def __str__(self):
        return f"{self.organization_id}:{self.name}"


class Membership(TimeStamped):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        ACCOUNTANT = "accountant", "Accountant"
        VIEWER = "viewer", "Viewer"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.ACCOUNTANT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "organization"], name="uniq_membership")]
