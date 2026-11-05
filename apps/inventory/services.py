"""Stock engine: the ONLY code that writes StockMovement. Stock may never go negative.

Costing/valuation is intentionally absent until the costing method is confirmed (see docs).
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import DocStatus, next_number

from .models import Item, Location, ProductionInput, ProductionOrder, StockMovement

Z = Decimal("0")
Q4 = Decimal("0.0001")


def qty(value):
    try:
        return Decimal(str(value)).quantize(Q4)
    except Exception:
        raise DomainError("Invalid quantity.", "invalid_quantity")


def default_location(org):
    loc, _ = Location.objects.get_or_create(organization=org, name="Main Warehouse", defaults={"kind": Location.Kind.WAREHOUSE})
    return loc


def on_hand(org, item, location):
    return StockMovement.objects.filter(organization=org, item=item, location=location).aggregate(s=Sum("quantity"))["s"] or Z


def _lock_items(org, item_ids):
    """Serialise concurrent stock changes per item (row locks, fixed order to avoid deadlocks)."""
    list(Item.objects.select_for_update().filter(organization=org, pk__in=sorted(set(item_ids))).order_by("pk"))


def _check_same_org(org, *objs):
    for o in objs:
        if o.organization_id != org.id:
            raise DomainError("Item or location does not belong to this organization.", "invalid_reference")


def move(org, user, item, location, quantity, mtype, date, ref_type="", ref_id=None, note="", valued=False):
    """Append one movement. A negative quantity is rejected if it would drive on-hand below zero."""
    quantity = qty(quantity)
    _check_same_org(org, item, location)
    if not valued and mtype not in (StockMovement.Type.TRANSFER_OUT, StockMovement.Type.TRANSFER_IN):
        from . import costing

        if costing.costing_on(org):
            if mtype in costing.UNVALUED_TYPES:
                raise DomainError("Production and van delivery are not supported while inventory costing is enabled.", "costing_unsupported")
            raise DomainError("Stock changes must go through the costing engine while inventory costing is enabled.", "costing_required")
    if quantity == 0:
        raise DomainError("Quantity must not be zero.", "invalid_quantity")
    if quantity < 0 and on_hand(org, item, location) + quantity < 0:
        raise DomainError(
            f"Insufficient stock of {item.sku} at {location.name}: on hand {on_hand(org, item, location)}, needed {-quantity}.",
            "insufficient_stock")
    return StockMovement.objects.create(organization=org, item=item, location=location, quantity=quantity, type=mtype,
                                        date=date, ref_type=ref_type, ref_id=ref_id, note=note, created_by=user)


@transaction.atomic
def adjust_stock(org, user, *, item, location, quantity, date, mtype="adjustment", note="", unit_cost=None):
    if mtype not in (StockMovement.Type.OPENING, StockMovement.Type.ADJUSTMENT):
        raise DomainError("Type must be opening or adjustment.", "invalid_type")
    if not item.is_active or not location.is_active:
        raise DomainError("Item and location must be active.", "inactive")
    from . import costing

    if costing.costing_on(org):
        return costing.adjust(org, user, item=item, location=location, quantity=quantity, date=date, mtype=mtype, note=note, unit_cost=unit_cost)
    _lock_items(org, [item.id])
    return move(org, user, item, location, quantity, mtype, date, "inventory.adjustment", None, note)


@transaction.atomic
def transfer_stock(org, user, *, item, source, destination, quantity, date, ref_type="", ref_id=None, note=""):
    quantity = qty(quantity)
    if quantity <= 0:
        raise DomainError("Transfer quantity must be greater than zero.", "invalid_quantity")
    if source.id == destination.id:
        raise DomainError("Source and destination must differ.", "same_location")
    _lock_items(org, [item.id])
    move(org, user, item, source, -quantity, StockMovement.Type.TRANSFER_OUT, date, ref_type, ref_id, note)
    move(org, user, item, destination, quantity, StockMovement.Type.TRANSFER_IN, date, ref_type, ref_id, note)


@retry_on_key_conflict
@transaction.atomic
def create_production(org, user, *, date, item, quantity, location, inputs, note="", idempotency_key=None):
    """inputs: [{"item": Item, "quantity": Decimal}]. Consumes raw materials and produces the finished good."""
    existing = find_existing(ProductionOrder, org, idempotency_key)
    if existing:
        return existing, False
    quantity = qty(quantity)
    _check_same_org(org, item, location)
    if item.kind != Item.Kind.FINISHED_GOOD:
        raise DomainError("The produced item must be a finished good.", "invalid_output_item")
    if quantity <= 0:
        raise DomainError("Produced quantity must be greater than zero.", "invalid_quantity")
    if not inputs:
        raise DomainError("At least one raw material must be consumed.", "no_inputs")
    seen = set()
    rows = []
    for i in inputs:
        raw, q = i["item"], qty(i["quantity"])
        _check_same_org(org, raw)
        if raw.kind != Item.Kind.RAW_MATERIAL:
            raise DomainError(f"{raw.sku} is not a raw material.", "invalid_input_item")
        if q <= 0:
            raise DomainError("Consumed quantities must be greater than zero.", "invalid_quantity")
        if raw.id in seen:
            raise DomainError(f"{raw.sku} is listed twice.", "duplicate_input")
        seen.add(raw.id)
        rows.append((raw, q))
    if not item.is_active or not location.is_active or any(not r.is_active for r, _ in rows):
        raise DomainError("Items and location must be active.", "inactive")

    _lock_items(org, [item.id, *seen])
    order = ProductionOrder.objects.create(
        organization=org, number=next_number(org, "inventory.production", "PRD"), date=date, item=item, quantity=quantity,
        location=location, note=note, idempotency_key=idempotency_key or None, created_by=user)
    for raw, q in rows:
        ProductionInput.objects.create(organization=org, order=order, item=raw, quantity=q)
        move(org, user, raw, location, -q, StockMovement.Type.PRODUCTION_OUT, date, "inventory.production", order.id, order.number)
    move(org, user, item, location, quantity, StockMovement.Type.PRODUCTION_IN, date, "inventory.production", order.id, order.number)
    return order, True


@transaction.atomic
def void_production(order, user=None):
    order = ProductionOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == DocStatus.VOID:
        raise DomainError("Production order is already void.", "already_void")
    org = order.organization
    _lock_items(org, [order.item_id, *order.inputs.values_list("item_id", flat=True)])
    # Take the finished goods back first: fails (and rolls everything back) if they were already moved/used.
    move(org, user, order.item, order.location, -order.quantity, StockMovement.Type.PRODUCTION_VOID, order.date,
         "inventory.production", order.id, f"Void {order.number}")
    for inp in order.inputs.select_related("item"):
        move(org, user, inp.item, order.location, inp.quantity, StockMovement.Type.PRODUCTION_VOID, order.date,
             "inventory.production", order.id, f"Void {order.number}")
    order.status = DocStatus.VOID
    order.save(update_fields=["status", "updated_at"])
    return order
