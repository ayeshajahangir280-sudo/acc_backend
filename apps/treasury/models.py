from django.db import models
from django.db.models import Q

from apps.core.models import DocumentBase, OrgOwned


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    BANK_TRANSFER = "bank_transfer", "Bank transfer"
    CHEQUE = "cheque", "Cheque"
    CARD = "card", "Card"
    OTHER = "other", "Other"


class CashDocument(DocumentBase):
    """Shared shape of Receipt and Payment. Concrete tables stay separate so each can evolve alone."""

    # Customer (receipt) / supplier (payment). If empty, counter_account must be given instead.
    party = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    counter_account = models.ForeignKey("ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cash_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    reference = models.CharField(max_length=100, blank=True)

    class Meta:
        abstract = True


class Receipt(CashDocument):
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_receipt_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_receipt_idem"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="receipt_amount_positive"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"]), models.Index(fields=["organization", "party", "date"])]


class Payment(CashDocument):
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_payment_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_payment_idem"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"]), models.Index(fields=["organization", "party", "date"])]


class ReceiptAllocation(OrgOwned):
    """Part of a receipt applied against one sales invoice. Unapplied = receipt.amount - sum(allocations)."""

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="receipt_allocations")
    amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["receipt", "invoice"], name="uniq_receipt_alloc"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="receipt_alloc_positive"),
        ]
        indexes = [models.Index(fields=["invoice"])]


class PaymentAllocation(OrgOwned):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey("purchases.PurchaseInvoice", on_delete=models.PROTECT, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["payment", "invoice"], name="uniq_payment_alloc"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_alloc_positive"),
        ]
        indexes = [models.Index(fields=["invoice"])]
