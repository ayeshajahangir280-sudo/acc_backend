"""Quotes, sales orders and purchase orders. Commercial documents only: they never touch the ledger.

Posting happens when an order/quote is converted into an invoice or bill through the normal sales/purchase
services (so period lock, branch rules, numbering and idempotency all apply to the resulting document).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import OrgOwned


class OrderStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACCEPTED = "accepted", "Accepted"      # quotes only
    DECLINED = "declined", "Declined"      # quotes only
    CONVERTED = "converted", "Converted"
    CANCELLED = "cancelled", "Cancelled"


class _OrderBase(OrgOwned):
    number = models.CharField(max_length=32)
    date = models.DateField()
    valid_until = models.DateField(null=True, blank=True)   # quote expiry / expected delivery
    party = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="+")
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    narration = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=OrderStatus.choices, default=OrderStatus.OPEN)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2)
    tax_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        abstract = True


class _ItemBase(OrgOwned):
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=18, decimal_places=2)
    tax = models.DecimalField(max_digits=18, decimal_places=2)
    gross = models.DecimalField(max_digits=18, decimal_places=2)
    # Quantity already turned into invoices/bills (partial conversion). remaining = quantity - invoiced_qty.
    invoiced_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    # Optional stock link; carried onto the invoice/bill on conversion so stock and cost move through the normal costing rules.
    item = models.ForeignKey("inventory.Item", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True


class SalesOrder(_OrderBase):
    class Kind(models.TextChoices):
        QUOTE = "quote", "Quote"
        ORDER = "order", "Sales Order"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    source = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived")  # quote -> order
    invoice = models.OneToOneField("sales.SalesInvoice", null=True, blank=True, on_delete=models.PROTECT, related_name="source_order")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_salesorder_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_salesorder_idem"),
        ]
        indexes = [models.Index(fields=["organization", "kind", "-date", "-id"])]


class SalesOrderItem(_ItemBase):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="items")


class SalesOrderInvoice(OrgOwned):
    """One row per invoice raised from an order (an order can be invoiced in several parts)."""

    order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="conversions")
    invoice = models.OneToOneField("sales.SalesInvoice", on_delete=models.PROTECT, related_name="order_link")


class PurchaseOrder(_OrderBase):
    invoice = models.OneToOneField("purchases.PurchaseInvoice", null=True, blank=True, on_delete=models.PROTECT, related_name="source_order")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_purchaseorder_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_purchaseorder_idem"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"])]


class PurchaseOrderItem(_ItemBase):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")


class PurchaseOrderInvoice(OrgOwned):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="conversions")
    invoice = models.OneToOneField("purchases.PurchaseInvoice", on_delete=models.PROTECT, related_name="order_link")
