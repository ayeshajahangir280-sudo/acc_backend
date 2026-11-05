from django.conf import settings
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
    # VAT on a counter-account receipt/payment (no party): `amount` is the cash total and includes this tax.
    tax_rate = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

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


class _CreditApplication(OrgOwned):
    """Moves surplus (credit) on one invoice to another open invoice of the same party.

    No ledger entries are created: the party's control-account balance is unchanged, only the invoice-level
    split moves. `amount` accumulates if the same pair is applied again.
    """

    date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        abstract = True


class SalesCreditApplication(_CreditApplication):
    from_invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="credit_given")
    to_invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="credit_received")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["from_invoice", "to_invoice"], name="uniq_sales_credit_pair"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="sales_credit_amount_positive"),
            models.CheckConstraint(condition=~Q(from_invoice=models.F("to_invoice")), name="sales_credit_not_self"),
        ]


class PurchaseCreditApplication(_CreditApplication):
    from_invoice = models.ForeignKey("purchases.PurchaseInvoice", on_delete=models.PROTECT, related_name="credit_given")
    to_invoice = models.ForeignKey("purchases.PurchaseInvoice", on_delete=models.PROTECT, related_name="credit_received")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["from_invoice", "to_invoice"], name="uniq_purchase_credit_pair"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="purchase_credit_amount_positive"),
            models.CheckConstraint(condition=~Q(from_invoice=models.F("to_invoice")), name="purchase_credit_not_self"),
        ]


class _CreditRefund(DocumentBase):
    """Cash paid back to a customer (sales) or received back from a supplier (purchases) for credit held on one invoice.

    Sales:     Dr Accounts Receivable (customer) / Cr cash.      Purchases: Dr cash / Cr Accounts Payable (supplier).
    It raises the invoice's outstanding towards zero, so it can never exceed the credit the invoice holds."""

    party = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="+")
    cash_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    reference = models.CharField(max_length=100, blank=True)

    class Meta:
        abstract = True


class SalesCreditRefund(_CreditRefund):
    invoice = models.ForeignKey("sales.SalesInvoice", on_delete=models.PROTECT, related_name="refunds")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_sales_refund_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_sales_refund_idem"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="sales_refund_positive"),
        ]
        indexes = [models.Index(fields=["invoice"])]


class PurchaseCreditRefund(_CreditRefund):
    invoice = models.ForeignKey("purchases.PurchaseInvoice", on_delete=models.PROTECT, related_name="refunds")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_purchase_refund_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_purchase_refund_idem"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="purchase_refund_positive"),
        ]
        indexes = [models.Index(fields=["invoice"])]
