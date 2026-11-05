import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import ApprovalDocumentBase, OrgOwned


def receipt_path(instance, filename):
    # Never trust the client file name for the stored path; keep only the extension.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"expense-receipts/org{instance.organization_id}/{instance.number}.{ext}"


class Expense(ApprovalDocumentBase):
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
    # Billable (re-chargeable) expense: charged to this customer on a sales invoice at cost, once.
    is_billable = models.BooleanField(default=False)
    customer = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    billed_invoice = models.ForeignKey("sales.SalesInvoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="billed_expenses")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_expense_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_expense_idem"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"])]


def attachment_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"expense-receipts/org{instance.organization_id}/{instance.expense.number}-{uuid.uuid4().hex[:12]}.{ext}"


class ExpenseReceipt(OrgOwned):
    """Additional receipt files of an expense (the original single `receipt` field keeps working)."""

    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path)
    name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=50)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
