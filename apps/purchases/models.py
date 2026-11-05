from django.db import models
from django.db.models import Q

from apps.core.models import ApprovalDocumentBase, OrgOwned


class PurchaseInvoice(ApprovalDocumentBase):
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        RETURN = "return", "Purchase Return"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INVOICE)
    party = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="+")
    original = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="returns")
    due_date = models.DateField(null=True, blank=True)  # defaults to invoice date in the service
    subtotal = models.DecimalField(max_digits=18, decimal_places=2)
    tax_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_purchase_number"),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_purchase_idem"
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "-date", "-id"]),
            models.Index(fields=["organization", "party", "date"]),
            models.Index(fields=["organization", "kind", "date"]),
        ]


class PurchaseItem(OrgOwned):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # percent, user-supplied
    net = models.DecimalField(max_digits=18, decimal_places=2)
    tax = models.DecimalField(max_digits=18, decimal_places=2)
    gross = models.DecimalField(max_digits=18, decimal_places=2)
    # Optional stock link (only usable once inventory costing is enabled). stock_value = ledger value moved with this line.
    item = models.ForeignKey("inventory.Item", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    stock_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

