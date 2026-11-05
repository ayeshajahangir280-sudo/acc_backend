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
    # Period lock: books are closed through this date (inclusive). No document dated on or before it can be
    # posted or voided. NULL = nothing locked. Enforced in apps.ledger.services, not in the UI.
    books_locked_through = models.DateField(null=True, blank=True)
    # Inventory costing (weighted average): NULL = off. Set once, by apps.inventory.costing.enable_costing; never unset.
    inventory_costing_since = models.DateField(null=True, blank=True)
    # Approval workflow (off by default = today's behaviour). When on, an expense / purchase invoice whose total is at or
    # above approval_min_amount is saved as "pending" and posts only when an owner/admin approves it.
    expense_approval_required = models.BooleanField(default=False)
    bill_approval_required = models.BooleanField(default=False)
    approval_min_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)

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


class BranchAccess(TimeStamped):
    """Grants a restricted member (accountant/viewer) access to one branch.

    Owners and admins never need rows here: they can reach every branch of their organization. A restricted member
    with no rows can reach no branch. Reads always join through Branch.organization, so a row that somehow pointed
    at another organization's branch would still grant nothing.
    """

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="branch_access")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="access")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["membership", "branch"], name="uniq_branch_access")]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.membership_id and self.branch_id and self.membership.organization_id != self.branch.organization_id:
            raise ValidationError("The branch belongs to a different organization than the member.")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class FiscalYearClose(TimeStamped):
    """Record of a fiscal-year close: who locked the books through which date, and the profit carried forward."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="fiscal_closes")
    year_start = models.DateField()
    year_end = models.DateField()
    net_profit = models.DecimalField(max_digits=18, decimal_places=2)
    previous_lock = models.DateField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    # Reopening keeps the record (it is part of the audit trail); only a close that has not been reopened counts.
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reopen_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "year_end"], condition=models.Q(reopened_at__isnull=True),
                                               name="uniq_active_fiscal_close_per_year")]
        ordering = ["-year_end", "-id"]


class PeriodLockChange(TimeStamped):
    """Audit trail of every change to Organization.books_locked_through (manual edit, fiscal close, fiscal reopen)."""

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual change"
        FISCAL_CLOSE = "fiscal_close", "Fiscal year closed"
        FISCAL_REOPEN = "fiscal_reopen", "Fiscal year reopened"

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="lock_changes")
    old_value = models.DateField(null=True, blank=True)
    new_value = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    reason = models.CharField(max_length=300, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["organization", "-id"])]
