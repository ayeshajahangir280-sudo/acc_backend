from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import DocStatus, OrgOwned


class Item(OrgOwned):
    class Kind(models.TextChoices):
        RAW_MATERIAL = "raw_material", "Raw material"
        FINISHED_GOOD = "finished_good", "Finished good"

    sku = models.CharField(max_length=40)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=14, choices=Kind.choices)
    unit = models.CharField(max_length=20, default="unit")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "sku"], name="uniq_item_sku")]
        indexes = [models.Index(fields=["organization", "kind", "name"])]


class Location(OrgOwned):
    """A place stock can sit: a warehouse, or a vehicle (created by the dispatch module)."""

    class Kind(models.TextChoices):
        WAREHOUSE = "warehouse", "Warehouse"
        VEHICLE = "vehicle", "Vehicle"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.WAREHOUSE)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_location_name")]


class StockMovement(OrgOwned):
    """Append-only stock ledger. On-hand quantity = SUM(quantity) per (item, location); never edited or deleted."""

    class Type(models.TextChoices):
        OPENING = "opening", "Opening balance"
        ADJUSTMENT = "adjustment", "Adjustment"
        PRODUCTION_OUT = "production_out", "Raw material consumed"
        PRODUCTION_IN = "production_in", "Finished goods produced"
        PRODUCTION_VOID = "production_void", "Production reversal"
        TRANSFER_OUT = "transfer_out", "Transfer out"
        TRANSFER_IN = "transfer_in", "Transfer in"
        VAN_DELIVERED = "van_delivered", "Delivered from van"
        SALE = "sale", "Sale"
        SALES_RETURN = "sales_return", "Sales return"
        PURCHASE = "purchase", "Purchase"
        PURCHASE_RETURN = "purchase_return", "Purchase return"

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="movements")
    quantity = models.DecimalField(max_digits=18, decimal_places=4)  # signed: + in, - out
    type = models.CharField(max_length=16, choices=Type.choices)
    date = models.DateField()
    ref_type = models.CharField(max_length=30, blank=True)
    ref_id = models.BigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(quantity=0), name="movement_nonzero")]
        indexes = [
            models.Index(fields=["organization", "item", "location"]),
            models.Index(fields=["organization", "-date", "-id"]),
            models.Index(fields=["organization", "ref_type", "ref_id"]),
        ]


class ProductionOrder(OrgOwned):
    """Raw materials consumed -> finished good produced, at one location."""

    number = models.CharField(max_length=32)
    date = models.DateField()
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")  # finished good
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.POSTED)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_production_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_production_idem"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="production_qty_positive"),
        ]
        indexes = [models.Index(fields=["organization", "-date", "-id"])]


class ProductionInput(OrgOwned):
    order = models.ForeignKey(ProductionOrder, on_delete=models.CASCADE, related_name="inputs")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")  # raw material
    quantity = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "item"], name="uniq_production_input"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="production_input_positive"),
        ]


class StockValuation(OrgOwned):
    """Append-only valuation ledger (exists only once inventory costing is enabled).

    One row per org-wide change in an item's quantity and value. Average cost = SUM(value) / SUM(quantity).
    Transfers between locations do not change org-wide quantity or value, so they have no row.
    SUM(value) over the organization must equal the INVENTORY account balance (see costing.reconcile).
    """

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="valuations")
    date = models.DateField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)   # signed
    value = models.DecimalField(max_digits=18, decimal_places=2)      # signed
    source_type = models.CharField(max_length=30)
    source_id = models.BigIntegerField(null=True, blank=True)
    movement = models.ForeignKey(StockMovement, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["organization", "item"])]
