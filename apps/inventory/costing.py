"""Inventory costing: weighted-average valuation, tied to the ledger.

Opt-in per organization (`inventory_costing_since`). Once on, every stock change that alters org-wide quantity is
valued and posted through the posting engine:

  purchase bill      Dr INVENTORY  (instead of PURCHASES)         stock in at the line's net amount
  purchase return    Cr INVENTORY at the original cost, difference to PURCHASE_RETURNS
  sales invoice      Dr COGS / Cr INVENTORY at the current average cost
  sales return       Dr INVENTORY / Cr COGS at the original sale's cost
  adjustment         Dr/Cr INVENTORY against STOCK_ADJUSTMENT (opening: EQUITY)
  transfer           no entry (org-wide value unchanged)

Production and van delivery are NOT valued yet and are refused while costing is on (see docs/ASSUMPTIONS.md).
Average cost = SUM(value)/SUM(quantity) of StockValuation rows, read under the item row lock.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.money import money
from apps.ledger.models import Account, AccountType
from apps.ledger.services import Line, post_journal, system_account

from . import services as stock
from .models import Item, StockMovement, StockValuation

Z = Decimal("0")
UNVALUED_TYPES = {StockMovement.Type.PRODUCTION_OUT, StockMovement.Type.PRODUCTION_IN, StockMovement.Type.PRODUCTION_VOID,
                  StockMovement.Type.VAN_DELIVERED}


def costing_on(org):
    from apps.organizations.models import Organization

    return Organization.objects.filter(pk=org.pk, inventory_costing_since__isnull=False).exists()


def position(org, item):
    a = StockValuation.objects.filter(organization=org, item=item).aggregate(q=Sum("quantity"), v=Sum("value"))
    return a["q"] or Z, a["v"] or Z


def outflow_value(org, item, qty):
    """Value of removing `qty` at the current average cost (the last unit takes the remaining value: no residue)."""
    q, v = position(org, item)
    if qty > q:
        raise DomainError(f"Insufficient stock of {item.sku}: on hand {q}, needed {qty}.", "insufficient_stock")
    return v if qty == q else money(v * qty / q)


def valued_move(org, user, item, location, quantity, mtype, date, *, value=None, ref_type="", ref_id=None, note=""):
    """Move stock and record its value. Caller must hold the item lock. Outflows default to the average cost.
    `value` is the signed ledger value (same sign as quantity); returns (movement, signed value)."""
    quantity = stock.qty(quantity)
    if value is None:
        if quantity >= 0:
            raise DomainError("An inflow needs an explicit value.", "value_required")
        value = -outflow_value(org, item, -quantity)
    value = money(value)
    mov = stock.move(org, user, item, location, quantity, mtype, date, ref_type, ref_id, note, valued=True)
    StockValuation.objects.create(organization=org, item=item, date=date, quantity=quantity, value=value,
                                  source_type=ref_type, source_id=ref_id, movement=mov)
    return mov, value


def lock(org, item_ids):
    stock._lock_items(org, item_ids)


def accounts(org):
    return {c: system_account(org, c) for c in ("INVENTORY", "COGS", "STOCK_ADJUSTMENT")}


def _free_code(org, want):
    code = int(want)
    while Account.objects.filter(organization=org, code=str(code)).exists():
        code += 1
    return str(code)


@transaction.atomic
def enable_costing(org, user, *, date, costs):
    """Turn costing on from `date`. `costs` = {item_id: unit_cost}; every item with stock on hand needs one.
    Posts one opening journal (Dr INVENTORY, Cr EQUITY) for the existing stock. Cannot be turned off."""
    from apps.organizations.models import Organization

    org_row = Organization.objects.select_for_update().get(pk=org.pk)
    if org_row.inventory_costing_since:
        raise DomainError("Inventory costing is already enabled.", "already_enabled")
    items = {i.id: i for i in Item.objects.filter(organization=org)}
    lock(org, items)
    onhand = {r["item_id"]: r["s"] for r in StockMovement.objects.filter(organization=org).values("item_id").annotate(s=Sum("quantity")) if r["s"]}
    missing = [items[i].sku for i in onhand if i not in costs]
    if missing:
        raise DomainError(f"Provide a unit cost for: {', '.join(sorted(missing))}.", "cost_required")
    for i, c in costs.items():
        if i not in items:
            raise DomainError("Unknown item in costs.", "invalid_reference")
        if Decimal(str(c)) < 0:
            raise DomainError("Unit cost cannot be negative.", "invalid_cost")
    for code, name, typ, want in (("INVENTORY", "Inventory", AccountType.ASSET, 1400), ("COGS", "Cost of Goods Sold", AccountType.EXPENSE, 5200),
                                  ("STOCK_ADJUSTMENT", "Stock Adjustments", AccountType.EXPENSE, 5300)):
        Account.objects.get_or_create(organization=org, system_code=code, defaults={"code": _free_code(org, want), "name": name, "type": typ})
    acc = accounts(org)
    rows, total = [], Z
    for iid, q in onhand.items():
        if q < 0:
            raise DomainError("Negative stock exists; fix it before enabling costing.", "negative_stock")
        v = money(q * Decimal(str(costs[iid])))
        rows.append((iid, q, v))
        total += v
    org_row.inventory_costing_since = date
    org_row.save(update_fields=["inventory_costing_since", "updated_at"])
    if total > 0:
        post_journal(org, date, [Line(acc["INVENTORY"], debit=total), Line(system_account(org, "EQUITY"), credit=total)],
                     "inventory.costing", None, "COSTING-OPEN", "Opening inventory valuation", None, user)
    for iid, q, v in rows:
        StockValuation.objects.create(organization=org, item=items[iid], date=date, quantity=q, value=v, source_type="inventory.costing")
    return {"since": date, "items_valued": len(rows), "opening_value": total}


@transaction.atomic
def adjust(org, user, *, item, location, quantity, date, mtype, note="", unit_cost=None):
    """Costed stock adjustment / opening entry with its journal."""
    quantity = stock.qty(quantity)
    lock(org, [item.id])
    acc = accounts(org)
    if quantity > 0:
        if unit_cost is None or Decimal(str(unit_cost)) < 0:
            raise DomainError("A unit cost is required to add stock while costing is enabled.", "cost_required")
        value = money(quantity * Decimal(str(unit_cost)))
    else:
        value = None
    mov, signed = valued_move(org, user, item, location, quantity, mtype, date, value=value, ref_type="inventory.adjustment", note=note)
    if signed:
        other = system_account(org, "EQUITY") if mtype == StockMovement.Type.OPENING else acc["STOCK_ADJUSTMENT"]
        lines = ([Line(acc["INVENTORY"], debit=signed), Line(other, credit=signed)] if signed > 0
                 else [Line(other, debit=-signed), Line(acc["INVENTORY"], credit=-signed)])
        post_journal(org, date, lines, "inventory.adjustment", mov.id, f"ADJ-{mov.id}", note, None, user)
    return mov


def reconcile(org):
    """Per-item valuation vs stock movements, and total valuation vs the INVENTORY ledger balance."""
    from apps.ledger.models import JournalLine

    rows = {}
    for r in StockValuation.objects.filter(organization=org).values("item_id").annotate(q=Sum("quantity"), v=Sum("value")):
        rows[r["item_id"]] = {"quantity": r["q"], "value": r["v"]}
    moved = {r["item_id"]: r["s"] for r in StockMovement.objects.filter(organization=org).values("item_id").annotate(s=Sum("quantity"))}
    items = {i.id: i for i in Item.objects.filter(organization=org, id__in=set(rows) | set(moved))}
    out = []
    for iid in sorted(set(rows) | set(moved), key=lambda i: items[i].name):
        r = rows.get(iid, {"quantity": Z, "value": Z})
        mq = moved.get(iid, Z)
        out.append({"item": iid, "sku": items[iid].sku, "name": items[iid].name, "quantity": r["quantity"], "value": r["value"],
                    "average_cost": (money(r["value"] / r["quantity"]) if r["quantity"] else Z), "movement_quantity": mq,
                    "quantity_difference": r["quantity"] - mq})
    total = sum((r["value"] for r in out), Z)
    inv = Account.objects.filter(organization=org, system_code="INVENTORY").first()
    ledger = Z
    if inv:
        a = JournalLine.objects.filter(account=inv).aggregate(d=Sum("debit"), c=Sum("credit"))
        ledger = (a["d"] or Z) - (a["c"] or Z)
    return {"items": out, "total_value": total, "ledger_balance": ledger, "difference": total - ledger,
            "quantity_mismatches": [r["sku"] for r in out if r["quantity_difference"] != 0]}


# ---- sales / purchase documents ------------------------------------------------------------------------------

MODES = {  # mode -> (stock direction +1 in / -1 out, movement type, cost basis)
    "sale": (-1, StockMovement.Type.SALE, "average"),
    "sales_return": (1, StockMovement.Type.SALES_RETURN, "original"),
    "purchase": (1, StockMovement.Type.PURCHASE, "net"),
    "purchase_return": (-1, StockMovement.Type.PURCHASE_RETURN, "original"),
}


def validate_rows(org, rows):
    """Item links on document lines need costing on and an active item of this organization."""
    linked = [r["item"] for r in rows if r.get("item") is not None]
    if not linked:
        return False
    if not costing_on(org):
        raise DomainError("Enable inventory costing before using items on invoice lines.", "costing_disabled")
    for it in linked:
        if it.organization_id != org.id or not it.is_active:
            raise DomainError("Item must be an active item of this organization.", "invalid_reference")
    return True


def _remaining(model, original, item, exclude):
    base = model.objects.filter(invoice=original, item=item).aggregate(q=Sum("quantity"), v=Sum("stock_value"))
    done = model.objects.filter(invoice__original=original, invoice__status="posted", item=item).exclude(invoice=exclude).aggregate(q=Sum("quantity"), v=Sum("stock_value"))
    if not base["q"]:
        raise DomainError(f"{item.sku} is not on the original invoice.", "item_not_on_original")
    return base["q"] - (done["q"] or Z), (base["v"] or Z) - (done["v"] or Z)


def apply_document(org, user, doc, objs, mode, date, original=None):
    """Move and value stock for the saved line objects (those with an item). Returns the total value moved.
    Sets stock_value on each line. Must run in the document's transaction, after the lines exist."""
    sign, mtype, basis = MODES[mode]
    tracked = [o for o in objs if o.item_id]
    if not tracked:
        return Z
    lock(org, {o.item_id for o in tracked})
    loc = stock.default_location(org)
    total = Z
    for o in tracked:
        q = stock.qty(o.quantity)
        if basis == "average":
            value = None                      # valued_move uses the average cost
        elif basis == "net":
            value = sign * o.net
        else:
            rq, rv = _remaining(type(o), original, o.item, doc)
            if q > rq:
                raise DomainError(f"{o.item.sku}: returning {q} but only {rq} remain on the original invoice.", "over_return_qty")
            value = sign * (rv if q == rq else money(rv * q / rq))
        mov, val = valued_move(org, user, o.item, loc, sign * q, mtype, date, value=value, ref_type=doc_ref(doc), ref_id=doc.id, note=doc.number)
        o.stock_value = abs(val)
        o.save(update_fields=["stock_value"])
        total += abs(val)
    return total


def doc_ref(doc):
    return f"{doc._meta.app_label}.{doc.kind}"


def reverse_document(org, user, doc, mode, date):
    """Undo apply_document for a voided document (each line at its recorded value)."""
    sign, mtype, _ = MODES[mode]
    rows = list(doc.items.filter(item__isnull=False).select_related("item"))
    if not rows:
        return
    lock(org, {r.item_id for r in rows})
    loc = stock.default_location(org)
    for r in rows:
        valued_move(org, user, r.item, loc, -sign * stock.qty(r.quantity), mtype, date, value=-sign * r.stock_value,
                    ref_type=doc_ref(doc), ref_id=doc.id, note=f"Void {doc.number}")
