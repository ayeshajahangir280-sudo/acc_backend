from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import DocStatus, OrgOwned


class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class Account(OrgOwned):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=10, choices=AccountType.choices)
    # Stable identifier the posting services use to find well-known accounts (e.g. "ACCOUNTS_RECEIVABLE").
    system_code = models.CharField(max_length=40, null=True, blank=True)
    is_cash_bank = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_account_code"),
            models.UniqueConstraint(
                fields=["organization", "system_code"], condition=Q(system_code__isnull=False), name="uniq_account_system_code"
            ),
        ]
        indexes = [models.Index(fields=["organization", "type"])]

    def __str__(self):
        return f"{self.code} {self.name}"


class JournalTransaction(OrgOwned):
    """Header of a balanced set of ledger lines. Immutable once written; corrections are reversals."""

    date = models.DateField()
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    source_type = models.CharField(max_length=30)  # e.g. "sales.invoice"
    source_id = models.BigIntegerField(null=True, blank=True)
    reference = models.CharField(max_length=64, blank=True)
    narration = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.POSTED)
    reversal_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["organization", "date"]), models.Index(fields=["organization", "source_type", "source_id"])]


class JournalLine(OrgOwned):
    transaction = models.ForeignKey(JournalTransaction, on_delete=models.CASCADE, related_name="lines")
    # Denormalized from the header so reports never need a join to filter by date/branch.
    date = models.DateField()
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    party = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    description = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)), name="line_one_sided_positive"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "account", "date"]),
            models.Index(fields=["organization", "party", "date"]),
            models.Index(fields=["organization", "date"]),
        ]
