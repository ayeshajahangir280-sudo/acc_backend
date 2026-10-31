from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import OrgOwned


class Vehicle(OrgOwned):
    name = models.CharField(max_length=120)
    plate_number = models.CharField(max_length=30, blank=True)
    driver_name = models.CharField(max_length=120, blank=True)
    # Stock carried on the van lives in its own inventory location (created with the vehicle).
    location = models.OneToOneField("inventory.Location", on_delete=models.PROTECT, related_name="vehicle")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="uniq_vehicle_name")]


class VanLoad(OrgOwned):
    class Status(models.TextChoices):
        LOADED = "loaded", "Loaded"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        CLOSED = "closed", "Closed"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=32)
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="loads")
    source_location = models.ForeignKey("inventory.Location", on_delete=models.PROTECT, related_name="+")
    assigned_to = models.CharField(max_length=120, blank=True)
    dispatch_at = models.DateTimeField()  # dispatch date and time
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.LOADED)
    note = models.CharField(max_length=300, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_vanload_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_vanload_idem"),
        ]
        indexes = [models.Index(fields=["organization", "-dispatch_at"]), models.Index(fields=["organization", "status"])]


class VanLoadLine(OrgOwned):
    load = models.ForeignKey(VanLoad, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey("inventory.Item", on_delete=models.PROTECT, related_name="+")
    quantity_loaded = models.DecimalField(max_digits=18, decimal_places=4)
    quantity_delivered = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    quantity_returned = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["load", "item"], name="uniq_vanload_item"),
            models.CheckConstraint(condition=Q(quantity_loaded__gt=0), name="vanload_loaded_positive"),
        ]
