from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import next_number
from apps.purchases import services as purchase_services
from apps.sales import services as sales_services

from .models import (OrderStatus, PurchaseOrder, PurchaseOrderInvoice, PurchaseOrderItem, SalesOrder, SalesOrderInvoice,
                     SalesOrderItem)

# side -> (order model, item model, party kinds, invoice service module)
SIDES = {
    "sales": (SalesOrder, SalesOrderItem, ("customer", "both"), sales_services),
    "purchases": (PurchaseOrder, PurchaseOrderItem, ("supplier", "both"), purchase_services),
}
LINKS = {"sales": SalesOrderInvoice, "purchases": PurchaseOrderInvoice}
PREFIX = {"quote": "QT", "order": "SO", "purchase": "PO"}


def _check(org, side, party, branch):
    kinds = SIDES[side][2]
    if party.organization_id != org.id or party.kind not in kinds or not party.is_active:
        raise DomainError(f"Party must be an active {kinds[0]} of this organization.", "invalid_party")
    if branch is not None and (branch.organization_id != org.id or not branch.is_active):
        raise DomainError("Branch must be an active branch of this organization.", "invalid_branch")


def _write_items(org, side, order, items):
    rows, sub, tax = SIDES[side][3]._prepare_items(items)
    from apps.inventory import costing

    costing.validate_rows(org, rows)  # a stock item on a line needs costing on and an active item of this organization
    item_model = SIDES[side][1]
    item_model.objects.bulk_create([item_model(organization=org, order=order, **r) for r in rows])
    return sub, tax


@retry_on_key_conflict
@transaction.atomic
def create_order(org, user, side, *, kind, party, date, items, branch=None, valid_until=None, narration="",
                 source=None, idempotency_key=None):
    model = SIDES[side][0]
    existing = find_existing(model, org, idempotency_key)
    if existing:
        return existing, False
    _check(org, side, party, branch)
    from apps.organizations.access import assert_can_post

    assert_can_post(org, user, branch)  # same branch rules as the invoice it will become
    if side == "purchases":
        kind = "purchase"
    elif kind not in ("quote", "order"):
        raise DomainError("kind must be quote or order.", "invalid_kind")
    if valid_until is not None and valid_until < date:
        raise DomainError("The valid-until date cannot be before the document date.", "invalid_due_date")
    extra = {"kind": kind, "source": source} if side == "sales" else {}
    order = model.objects.create(
        organization=org, number=next_number(org, f"orders.{side}.{kind}", PREFIX[kind]), date=date, valid_until=valid_until,
        party=party, branch=branch, narration=narration, subtotal=0, total=0, idempotency_key=idempotency_key or None,
        created_by=user, **extra)
    sub, tax = _write_items(org, side, order, items)
    order.subtotal, order.tax_total, order.total = sub, tax, sub + tax
    order.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])
    return order, True


def _locked(side, order):
    return SIDES[side][0].objects.select_for_update().get(pk=order.pk)


@transaction.atomic
def update_order(org, side, order, *, items, date=None, valid_until=None, narration=None):
    """Replace the lines of an order that has not been converted/cancelled (non-ledger documents may be edited)."""
    order = _locked(side, order)
    if order.status not in (OrderStatus.OPEN, OrderStatus.ACCEPTED):
        raise DomainError("Only open documents can be edited.", "not_editable")
    if order.items.filter(invoiced_qty__gt=0).exists():
        raise DomainError("Part of this document has been invoiced and it can no longer be edited.", "not_editable")
    if date is not None:
        order.date = date
    if valid_until is not None:
        order.valid_until = valid_until
    if narration is not None:
        order.narration = narration
    if order.valid_until and order.valid_until < order.date:
        raise DomainError("The valid-until date cannot be before the document date.", "invalid_due_date")
    order.items.all().delete()
    sub, tax = _write_items(org, side, order, items)
    order.subtotal, order.tax_total, order.total = sub, tax, sub + tax
    order.save()
    return order


@transaction.atomic
def set_status(side, order, new):
    order = _locked(side, order)
    allowed = {
        "accepted": (OrderStatus.OPEN,), "declined": (OrderStatus.OPEN, OrderStatus.ACCEPTED),
        "cancelled": (OrderStatus.OPEN, OrderStatus.ACCEPTED),
    }[new]
    if new in ("accepted", "declined") and (side != "sales" or order.kind != "quote"):
        raise DomainError("Only quotes can be accepted or declined.", "not_a_quote")
    if order.status not in allowed:
        raise DomainError(f"A {order.status} document cannot be made {new}.", "invalid_transition")
    order.status = new
    order.save(update_fields=["status", "updated_at"])
    return order


def _items_payload(order):
    return [{"description": i.description, "quantity": i.quantity, "unit_price": i.unit_price, "tax_rate": i.tax_rate, "item": i.item}
            for i in order.items.select_related("item")]


@transaction.atomic
def convert(org, user, side, order, *, to, date=None, due_date=None, lines=None, idempotency_key=None):
    """Convert to an invoice/bill (posting it) or, for a quote, to a sales order. One conversion per document."""
    order = _locked(side, order)
    if order.status not in (OrderStatus.OPEN, OrderStatus.ACCEPTED):
        raise DomainError(f"A {order.status} document cannot be converted.", "invalid_transition")
    if lines is not None and to != "invoice":
        raise DomainError("Only conversion to an invoice or bill can be partial.", "invalid_conversion")
    today = timezone.localdate()
    if to == "order":
        if side != "sales" or order.kind != "quote":
            raise DomainError("Only a quote can be converted to a sales order.", "invalid_conversion")
        new, _ = create_order(org, user, "sales", kind="order", party=order.party, date=date or today, items=_items_payload(order),
                              branch=order.branch, narration=order.narration, source=order,
                              idempotency_key=f"convert-quote-{order.pk}")
        order.status = OrderStatus.CONVERTED
        order.save(update_fields=["status", "updated_at"])
        return new
    if to != "invoice":
        raise DomainError("Convert to 'invoice' (or 'order' for a quote).", "invalid_conversion")
    return _invoice(org, user, side, order, lines=lines, idempotency_key=idempotency_key, date=date or today, due_date=due_date)


def _invoice(org, user, side, order, *, lines, idempotency_key, date, due_date):
    """Invoice the whole remainder (lines=None) or chosen quantities of an order. Caller holds the order row lock.

    Quantities are tracked per line (invoiced_qty), so an order can be billed in several parts and never over-billed.
    Partial conversions need a client idempotency key: a repeated request returns the same invoice and changes nothing.
    """
    svc = SIDES[side][3]
    link_model = LINKS[side]
    items = list(order.items.select_related("item").order_by("id"))
    by_id = {i.pk: i for i in items}
    if lines is None:
        chosen = [(i, i.quantity - i.invoiced_qty) for i in items if i.quantity - i.invoiced_qty > 0]
        key = f"convert-{side}-{order.pk}" + (f"-{idempotency_key}" if idempotency_key else "")
    else:
        if not idempotency_key:
            raise DomainError("A partial conversion needs an idempotency key.", "idempotency_required")
        if not lines:
            raise DomainError("Choose at least one line to convert.", "no_lines")
        seen, chosen = set(), []
        for ln in lines:
            item = by_id.get(ln.get("item"))
            if item is None:
                raise DomainError("A chosen line does not belong to this document.", "line_not_on_order")
            if item.pk in seen:
                raise DomainError("A line can be chosen only once.", "duplicate_line")
            seen.add(item.pk)
            qty = Decimal(str(ln.get("quantity")))
            if qty <= 0:
                raise DomainError("Quantity must be greater than zero.", "invalid_quantity")
            if qty > item.quantity - item.invoiced_qty:
                raise DomainError(f"Only {item.quantity - item.invoiced_qty} of '{item.description}' is left to convert.", "over_convert")
            chosen.append((item, qty))
        key = f"convert-{side}-{order.pk}-{idempotency_key}"
    if not chosen:
        raise DomainError("Nothing is left to convert on this document.", "nothing_to_convert")
    payload = [{"description": i.description, "quantity": q, "unit_price": i.unit_price, "tax_rate": i.tax_rate, "item": i.item}
               for i, q in chosen]
    doc, created = svc.create_document(
        org, user, kind="invoice", party=order.party, date=date, items=payload, branch=order.branch,
        narration=order.narration or order.number, due_date=due_date, idempotency_key=key)
    if created:
        for item, qty in chosen:
            item.invoiced_qty += qty
            item.save(update_fields=["invoiced_qty", "updated_at"])
        link_model.objects.create(organization=org, order=order, invoice=doc)
        if not order.conversions.exclude(invoice=doc).exists():
            order.invoice = doc  # the first (or only) invoice, kept for older clients
        if all(i.invoiced_qty >= i.quantity for i in items):
            order.status = OrderStatus.CONVERTED
        order.save(update_fields=["status", "invoice", "updated_at"])
    return doc
