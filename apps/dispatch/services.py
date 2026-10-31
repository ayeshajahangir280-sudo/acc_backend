"""Van loading workflow. Moves stock only through inventory.services; never touches accounting.

Lifecycle: loaded -> out_for_delivery -> closed, or loaded -> cancelled.
Closing records, per item, how much was delivered (leaves the van) and how much came back (returns to the source
location). Whatever is neither delivered nor returned stays on the van and is reported as `remaining`.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import next_number
from apps.inventory import services as stock
from apps.inventory.models import Location, StockMovement

from .models import VanLoad, VanLoadLine, Vehicle

Z = Decimal("0")


@transaction.atomic
def create_vehicle(org, *, name, plate_number="", driver_name=""):
    loc = Location.objects.create(organization=org, name=f"Van: {name}", kind=Location.Kind.VEHICLE)
    return Vehicle.objects.create(organization=org, name=name, plate_number=plate_number, driver_name=driver_name, location=loc)


@retry_on_key_conflict
@transaction.atomic
def create_load(org, user, *, vehicle, lines, dispatch_at, source_location=None, assigned_to="", note="", idempotency_key=None):
    """lines: [{"item": Item, "quantity": Decimal}]. Moves stock source -> van immediately."""
    existing = find_existing(VanLoad, org, idempotency_key)
    if existing:
        return existing, False
    source = source_location or stock.default_location(org)
    if vehicle.organization_id != org.id or not vehicle.is_active:
        raise DomainError("Vehicle must be an active vehicle of this organization.", "invalid_vehicle")
    if source.organization_id != org.id or source.kind != Location.Kind.WAREHOUSE or not source.is_active:
        raise DomainError("Source must be an active warehouse of this organization.", "invalid_source")
    if not lines:
        raise DomainError("Load at least one product.", "no_lines")
    seen, rows = set(), []
    for ln in lines:
        item, q = ln["item"], stock.qty(ln["quantity"])
        if item.organization_id != org.id or not item.is_active:
            raise DomainError("Item must be an active item of this organization.", "invalid_item")
        if q <= 0:
            raise DomainError("Loaded quantities must be greater than zero.", "invalid_quantity")
        if item.id in seen:
            raise DomainError(f"{item.sku} is listed twice.", "duplicate_item")
        seen.add(item.id)
        rows.append((item, q))

    load = VanLoad.objects.create(
        organization=org, number=next_number(org, "dispatch.load", "VL"), vehicle=vehicle, source_location=source,
        assigned_to=assigned_to or vehicle.driver_name, dispatch_at=dispatch_at, note=note,
        idempotency_key=idempotency_key or None, created_by=user)
    for item, q in rows:
        VanLoadLine.objects.create(organization=org, load=load, item=item, quantity_loaded=q)
        stock.transfer_stock(org, user, item=item, source=source, destination=vehicle.location, quantity=q,
                             date=timezone.localdate(dispatch_at), ref_type="dispatch.load", ref_id=load.id, note=load.number)
    return load, True


def _lock(load):
    return VanLoad.objects.select_for_update().get(pk=load.pk)


@transaction.atomic
def dispatch(load):
    load = _lock(load)
    if load.status != VanLoad.Status.LOADED:
        raise DomainError("Only a loaded van can be dispatched.", "invalid_transition")
    load.status = VanLoad.Status.OUT_FOR_DELIVERY
    load.save(update_fields=["status", "updated_at"])
    return load


@transaction.atomic
def cancel(load, user=None):
    load = _lock(load)
    if load.status != VanLoad.Status.LOADED:
        raise DomainError("Only a load that has not left can be cancelled.", "invalid_transition")
    for ln in load.lines.select_related("item"):
        stock.transfer_stock(load.organization, user, item=ln.item, source=load.vehicle.location, destination=load.source_location,
                             quantity=ln.quantity_loaded, date=timezone.localdate(), ref_type="dispatch.load", ref_id=load.id,
                             note=f"Cancel {load.number}")
    load.status = VanLoad.Status.CANCELLED
    load.save(update_fields=["status", "updated_at"])
    return load


@transaction.atomic
def close(load, user, outcomes):
    """outcomes: [{"item": Item, "delivered": Decimal, "returned": Decimal}]; items omitted count as 0/0."""
    load = _lock(load)
    if load.status != VanLoad.Status.OUT_FOR_DELIVERY:
        raise DomainError("Only a van that is out for delivery can be closed.", "invalid_transition")
    by_item = {}
    for o in outcomes:
        if o["item"].id in by_item:
            raise DomainError(f"{o['item'].sku} is listed twice.", "duplicate_item")
        by_item[o["item"].id] = o
    lines = {ln.item_id: ln for ln in load.lines.select_related("item")}
    if set(by_item) - set(lines):
        raise DomainError("Outcome given for a product that was not loaded.", "unknown_item")
    today, org = timezone.localdate(), load.organization
    for item_id, ln in lines.items():
        o = by_item.get(item_id, {})
        delivered, returned = stock.qty(o.get("delivered", 0)), stock.qty(o.get("returned", 0))
        if delivered < 0 or returned < 0:
            raise DomainError("Quantities cannot be negative.", "invalid_quantity")
        if delivered + returned > ln.quantity_loaded:
            raise DomainError(f"{ln.item.sku}: delivered + returned ({delivered + returned}) exceeds loaded ({ln.quantity_loaded}).", "over_closed")
        if delivered:
            stock.move(org, user, ln.item, load.vehicle.location, -delivered, StockMovement.Type.VAN_DELIVERED, today,
                       "dispatch.load", load.id, load.number)
        if returned:
            stock.transfer_stock(org, user, item=ln.item, source=load.vehicle.location, destination=load.source_location,
                                 quantity=returned, date=today, ref_type="dispatch.load", ref_id=load.id, note=f"Return {load.number}")
        ln.quantity_delivered, ln.quantity_returned = delivered, returned
        ln.save(update_fields=["quantity_delivered", "quantity_returned", "updated_at"])
    load.status, load.closed_at = VanLoad.Status.CLOSED, timezone.now()
    load.save(update_fields=["status", "closed_at", "updated_at"])
    return load
