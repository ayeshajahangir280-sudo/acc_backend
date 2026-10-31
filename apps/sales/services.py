from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.models import DocStatus, next_number
from apps.core.money import line_amounts
from apps.ledger.services import Line, post_journal, system_account, void_journal

from .models import SalesInvoice, SalesItem


def _prepare_items(items):
    if not items:
        raise DomainError("At least one item is required.", "no_items")
    rows, sub, tax = [], Decimal("0"), Decimal("0")
    for it in items:
        qty, price, rate = Decimal(it["quantity"]), Decimal(it["unit_price"]), Decimal(it.get("tax_rate") or 0)
        if qty <= 0 or price < 0 or not (0 <= rate <= 100):
            raise DomainError("Invalid item quantity, price or tax rate.", "invalid_item")
        net, t, gross = line_amounts(qty, price, rate)
        rows.append(dict(description=it["description"], quantity=qty, unit_price=price, tax_rate=rate, net=net, tax=t, gross=gross))
        sub += net
        tax += t
    if sub + tax <= 0:
        raise DomainError("Document total must be greater than zero.", "zero_total")
    return rows, sub, tax


@transaction.atomic
def create_document(org, user, *, kind, party, date, items, branch=None, narration="", original=None, idempotency_key=None, due_date=None):
    """Create and post a sales invoice or sales return. Returns (document, created)."""
    if idempotency_key:
        existing = SalesInvoice.objects.filter(organization=org, idempotency_key=idempotency_key).first()
        if existing:
            return existing, False
    if party.organization_id != org.id or party.kind not in ("customer", "both") or not party.is_active:
        raise DomainError("Party must be an active customer of this organization.", "invalid_party")

    if due_date is not None and due_date < date:
        raise DomainError("Due date cannot be before the invoice date.", "invalid_due_date")
    rows, subtotal, tax_total = _prepare_items(items)
    total = subtotal + tax_total

    if kind == SalesInvoice.Kind.RETURN:
        if original is None:
            raise DomainError("A sales return must reference the original invoice.", "original_required")
        original = SalesInvoice.objects.select_for_update().get(pk=original.pk)
        if (original.organization_id != org.id or original.kind != SalesInvoice.Kind.INVOICE
                or original.status != DocStatus.POSTED or original.party_id != party.id):
            raise DomainError("Original must be a posted invoice of the same customer.", "invalid_original")
        returned = original.returns.filter(status=DocStatus.POSTED).aggregate(s=Sum("total"))["s"] or Decimal("0")
        if returned + total > original.total:
            raise DomainError(f"Return exceeds the original invoice. Returnable: {original.total - returned}.", "over_return")
    elif original is not None:
        raise DomainError("Only returns can reference an original invoice.", "invalid_original")

    prefix = "SI" if kind == SalesInvoice.Kind.INVOICE else "SR"
    doc = SalesInvoice.objects.create(
        organization=org, number=next_number(org, f"sales.{kind}", prefix), date=date, branch=branch, narration=narration,
        kind=kind, party=party, original=original, due_date=due_date or date, subtotal=subtotal, tax_total=tax_total, total=total,
        idempotency_key=idempotency_key or None, created_by=user,
    )
    SalesItem.objects.bulk_create([SalesItem(organization=org, invoice=doc, **r) for r in rows])

    ar, rev, ret, tax_acc = (system_account(org, c) for c in ("ACCOUNTS_RECEIVABLE", "SALES", "SALES_RETURNS", "TAX_OUTPUT"))
    z = Decimal("0")
    if kind == SalesInvoice.Kind.INVOICE:
        lines = [Line(ar, debit=total, party=party), Line(rev, credit=subtotal)]
        if tax_total:
            lines.append(Line(tax_acc, credit=tax_total))
    else:
        lines = [Line(ar, credit=total, party=party), Line(ret, debit=subtotal)]
        if tax_total:
            lines.append(Line(tax_acc, debit=tax_total))
    txn = post_journal(org, date, lines, f"sales.{kind}", doc.id, doc.number, narration, branch, user)
    doc.journal = txn
    doc.save(update_fields=["journal", "updated_at"])
    return doc, True


@transaction.atomic
def void_document(doc, user=None):
    doc = SalesInvoice.objects.select_for_update().get(pk=doc.pk)
    if doc.status == DocStatus.VOID:
        raise DomainError("Document is already void.", "already_void")
    if doc.kind == SalesInvoice.Kind.INVOICE and doc.returns.filter(status=DocStatus.POSTED).exists():
        raise DomainError("Void the sales returns against this invoice first.", "has_returns")
    if doc.kind == "invoice" and doc.receipt_allocations.filter(**{"receipt__status": "posted"}).exists():
        raise DomainError("Void or reduce the receipts/payments allocated to this invoice first.", "has_allocations")
    void_journal(doc.journal, user)
    doc.status = DocStatus.VOID
    doc.save(update_fields=["status", "updated_at"])
    return doc
