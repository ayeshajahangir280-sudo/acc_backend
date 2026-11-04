from django.db import models
from django.db.models import Q

from apps.core.models import DocumentBase


def receipt_path(instance, filename):
    # Never trust the client file name for the stored path; keep only the extension.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"expense-receipts/org{instance.organization_id}/{instance.number}.{ext}"


class Expense(DocumentBase):
    """A money-out document without a purchase invoice (fuel, rent, bank fees...). Posts Dr expense (+ input VAT) / Cr paid-through."""

    expense_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    paid_through = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=100, blank=True)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    amount_includes_tax = models.BooleanField(default=False)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2)
    tax_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2)
    receipt = models.FileField(upload_to=receipt_path, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_expense_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_expense_idem"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"])]
