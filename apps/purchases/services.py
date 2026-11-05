from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core import approvals
from apps.core.models import ApprovalStatus, DocStatus, next_number
from apps.core.money import line_amounts
from apps.ledger.services import Line, post_journal, system_account, void_journal

from .models import PurchaseInvoice, PurchaseItem


def _prepare_items(items):
    if not items:
        raise DomainError("At least one item is required.", "no_items")
    rows, sub, tax = [], Decimal("0"), Decimal("0")
    for it in items:
        qty, price, rate = Decimal(it["quantity"]), Decimal(it["unit_price"]), Decimal(it.get("tax_rate") or 0)
        if qty <= 0 or price < 0 or not (0 <= rate <= 100):
            raise DomainError("Invalid item quantity, price or tax rate.", "invalid_item")
        net, t, gross = line_amounts(qty, price, rate)
        rows.append(dict(description=it["description"], quantity=qty, unit_price=price, tax_rate=rate, net=net, tax=t, gross=gross,
                         item=it.get("item")))
        sub += net
        tax += t
    if sub + tax <= 0:
        raise DomainError("Document total must be greater than zero.", "zero_total")
    return rows, sub, tax


@retry_on_key_conflict
@transaction.atomic
def create_document(org, user, *, kind, party, date, items, branch=None, narration="", original=None, idempotency_key=None, due_date=None):
    """Create and post a purchase invoice or purchase return. Returns (document, created)."""
    existing = find_existing(PurchaseInvoice, org, idempotency_key)
    if existing:
        return existing, False
    if party.organization_id != org.id or party.kind not in ("supplier", "both") or not party.is_active:
        raise DomainError("Party must be an active supplier of this organization.", "invalid_party")

    if due_date is not None and due_date < date:
        raise DomainError("Due date cannot be before the invoice date.", "invalid_due_date")
    rows, subtotal, tax_total = _prepare_items(items)
    total = subtotal + tax_total
    from apps.inventory import costing

    tracked = costing.validate_rows(org, rows)

    if kind == PurchaseInvoice.Kind.RETURN:
        if original is None:
            raise DomainError("A Purchase Return must reference the original invoice.", "original_required")
        original = PurchaseInvoice.objects.select_for_update().get(pk=original.pk)
        if (original.organization_id != org.id or original.kind != PurchaseInvoice.Kind.INVOICE
                or original.status != DocStatus.POSTED or original.party_id != party.id):
            raise DomainError("Original must be a posted invoice of the same supplier.", "invalid_original")
        if (branch.id if branch else None) != original.branch_id:
            raise DomainError("A return must be posted to the same branch as the original invoice.", "branch_mismatch")
        returned = original.returns.filter(status=DocStatus.POSTED).aggregate(s=Sum("total"))["s"] or Decimal("0")
        if returned + total > original.total:
            raise DomainError(f"Return exceeds the original invoice. Returnable: {original.total - returned}.", "over_return")
    elif original is not None:
        raise DomainError("Only returns can reference an original invoice.", "invalid_original")

    prefix = "PI" if kind == PurchaseInvoice.Kind.INVOICE else "PR"
    pending = kind == PurchaseInvoice.Kind.INVOICE and approvals.needs_approval(org, "bill", total)
    if pending:  # fail early on things approval cannot fix; the lock is checked again when it posts
        from apps.ledger.services import assert_period_open
        from apps.organizations.access import assert_can_post

        assert_period_open(org, date)
        assert_can_post(org, user, branch)
    doc = PurchaseInvoice.objects.create(status=ApprovalStatus.PENDING if pending else ApprovalStatus.POSTED,
        organization=org, number=next_number(org, f"purchases.{kind}", prefix), date=date, branch=branch, narration=narration,
        kind=kind, party=party, original=original, due_date=due_date or date, subtotal=subtotal, tax_total=tax_total, total=total,
        idempotency_key=idempotency_key or None, created_by=user,
    )
    if tracked:
        objs = [PurchaseItem.objects.create(organization=org, invoice=doc, **r) for r in rows]
    else:
        PurchaseItem.objects.bulk_create([PurchaseItem(organization=org, invoice=doc, **r) for r in rows])
        objs = []

    if pending:
        return doc, True
    _post_document(org, user, doc, objs, tracked)
    return doc, True


def _post_document(org, user, doc, objs, tracked):
    """Stock movement (when items are linked) and the journal. Runs at creation, or at approval for a pending bill."""
    kind, total, subtotal, tax_total, party, date, narration, branch = (
        doc.kind, doc.total, doc.subtotal, doc.tax_total, doc.party, doc.date, doc.narration, doc.branch)
    original = doc.original
    from apps.inventory import costing

    ap, exp, ret, tax_acc = (system_account(org, c) for c in ("ACCOUNTS_PAYABLE", "PURCHASES", "PURCHASE_RETURNS", "TAX_INPUT"))
    moved = Decimal("0")
    if tracked:
        moved = costing.apply_document(org, user, doc, objs, "purchase" if kind == PurchaseInvoice.Kind.INVOICE else "purchase_return", date, original)
    inv_acc = system_account(org, "INVENTORY") if moved else None
    if kind == PurchaseInvoice.Kind.INVOICE:
        lines = [Line(ap, credit=total, party=party)]
        if subtotal - moved:
            lines.append(Line(exp, debit=subtotal - moved))
        if moved:
            lines.append(Line(inv_acc, debit=moved))
        if tax_total:
            lines.append(Line(tax_acc, debit=tax_total))
    else:
        lines = [Line(ap, debit=total, party=party)]
        if moved:
            lines.append(Line(inv_acc, credit=moved))
        diff = subtotal - moved  # return price vs original cost: variance goes to purchase returns
        if diff > 0:
            lines.append(Line(ret, credit=diff))
        elif diff < 0:
            lines.append(Line(ret, debit=-diff))
        if tax_total:
            lines.append(Line(tax_acc, credit=tax_total))
    txn = post_journal(org, date, lines, f"purchases.{kind}", doc.id, doc.number, narration, branch, user)
    doc.journal = txn
    doc.save(update_fields=["journal", "updated_at"])


@transaction.atomic
def approve_document(doc, user, membership):
    doc = PurchaseInvoice.objects.select_for_update().get(pk=doc.pk)
    approvals.check_can_decide(membership, doc)
    approvals.check_self_approval(membership, user, doc)
    objs = list(doc.items.select_related("item"))
    tracked = any(o.item_id for o in objs)
    _post_document(doc.organization, user, doc, objs, tracked)  # period lock, branch rules and costing apply now
    approvals.mark_approved(doc, user)
    doc.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return doc


@transaction.atomic
def reject_document(doc, user, membership, reason):
    doc = PurchaseInvoice.objects.select_for_update().get(pk=doc.pk)
    approvals.check_can_decide(membership, doc)
    approvals.mark_rejected(doc, user, reason)
    doc.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])
    return doc


@transaction.atomic
def void_document(doc, user=None):
    doc = PurchaseInvoice.objects.select_for_update().get(pk=doc.pk)
    if doc.status == DocStatus.VOID:
        raise DomainError("Document is already void.", "already_void")
    if doc.status != DocStatus.POSTED:
        raise DomainError("Only posted documents can be voided; a pending bill can be rejected instead.", "not_posted")
    if doc.kind == PurchaseInvoice.Kind.INVOICE and doc.returns.filter(status=DocStatus.POSTED).exists():
        raise DomainError("Void the Purchase Returns against this invoice first.", "has_returns")
    if doc.kind == "invoice" and doc.payment_allocations.filter(**{"payment__status": "posted"}).exists():
        raise DomainError("Void or reduce the receipts/payments allocated to this invoice first.", "has_allocations")
    from apps.treasury import credits

    if doc.kind == "invoice":
        credits.assert_no_credit_links("payment", doc)
    elif doc.original_id:
        credits.lock_invoices(doc.organization, "payment", [doc.original_id])
    void_journal(doc.journal, user)
    if doc.items.filter(item__isnull=False).exists():
        from apps.inventory import costing

        costing.reverse_document(doc.organization, user, doc, "purchase" if doc.kind == "invoice" else "purchase_return", doc.date)
    doc.status = DocStatus.VOID
    doc.save(update_fields=["status", "updated_at"])
    if doc.original_id:
        credits.assert_credit_consistent(doc.organization, "payment", [doc.original_id])
    return doc

