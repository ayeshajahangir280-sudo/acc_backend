from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.models import OrgOwned


class BankStatement(OrgOwned):
    """An imported bank statement for one cash/bank account. Reconciling it never writes to the ledger."""

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        RECONCILED = "reconciled", "Reconciled"

    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    file_name = models.CharField(max_length=200, blank=True)
    file_hash = models.CharField(max_length=64)
    period_start = models.DateField()
    period_end = models.DateField()
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2)
    closing_balance = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    reconciled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "account", "file_hash"], name="uniq_statement_file_per_account")]
        indexes = [models.Index(fields=["organization", "account", "-period_end"])]


class StatementLine(OrgOwned):
    """One bank movement. amount > 0 is money in (a deposit), amount < 0 is money out."""

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")  # denormalised for the uniqueness rule
    row = models.PositiveIntegerField()
    date = models.DateField()
    description = models.CharField(max_length=300, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    # sha256 of (date, amount, description, reference, n-th identical row in its file). The same bank movement imported
    # again through an overlapping statement produces the same fingerprint and is caught.
    fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "account", "fingerprint"], name="uniq_statement_line_fingerprint"),
            models.UniqueConstraint(fields=["statement", "row"], name="uniq_statement_row"),
            models.CheckConstraint(condition=~Q(amount=0), name="statement_line_nonzero"),
        ]
        ordering = ["date", "row"]


class StatementMatch(OrgOwned):
    """Part (or all) of a statement line cleared against part (or all) of a ledger line. Positive amount; sign is implied."""

    line = models.ForeignKey(StatementLine, on_delete=models.CASCADE, related_name="matches")
    journal_line = models.ForeignKey("ledger.JournalLine", on_delete=models.PROTECT, related_name="statement_matches")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    matched_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    method = models.CharField(max_length=10, default="manual")  # manual | auto

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["line", "journal_line"], name="uniq_match_pair"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="match_amount_positive"),
        ]
