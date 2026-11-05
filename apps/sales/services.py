from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
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
        rows.append(dict(description=it["description"], quantity=qty, unit_price=price, tax_rate=rate, net=net, tax=t, gross=gross,
                         item=it.get("item")))
        sub += net
        tax += t
    if sub + tax <= 0:
        raise DomainError("Document total must be greater than zero.", "zero_total")
    return rows, sub, tax


@retry_on_key_conflict
@transaction.atomic
def create_document(org, user, *, kind, party, date, items, branch=None, narration="", original=None, idempotency_key=None, due_date=None, billable_expenses=None):
    """Create and post a sales invoice or sales return. Returns (document, created).

    billable_expenses: posted, unbilled expenses flagged billable for this customer; each becomes one line at its cost."""
    existing = find_existing(SalesInvoice, org, idempotency_key)
    if existing:
        return existing, False
    if party.organization_id != org.id or party.kind not in ("customer", "both") or not party.is_active:
        raise DomainError("Party must be an active customer of this organization.", "invalid_party")

    if due_date is not None and due_date < date:
        raise DomainError("Due date cannot be before the invoice date.", "invalid_due_date")
    billed = _billable_lines(org, party, kind, billable_expenses)
    rows, subtotal, tax_total = _prepare_items(list(items) + [line for _, line in billed])
    total = subtotal + tax_total
    from apps.inventory import costing

    tracked = costing.validate_rows(org, rows)

    if kind == SalesInvoice.Kind.RETURN:
        if original is None:
            raise DomainError("A sales return must reference the original invoice.", "original_required")
        original = SalesInvoice.objects.select_for_update().get(pk=original.pk)
        if (original.organization_id != org.id or original.kind != SalesInvoice.Kind.INVOICE
                or original.status != DocStatus.POSTED or original.party_id != party.id):
            raise DomainError("Original must be a posted invoice of the same customer.", "invalid_original")
        if (branch.id if branch else None) != original.branch_id:
            raise DomainError("A return must be posted to the same branch as the original invoice.", "branch_mismatch")
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
    if tracked:
        objs = [SalesItem.objects.create(organization=org, invoice=doc, **r) for r in rows]
    else:
        SalesItem.objects.bulk_create([SalesItem(organization=org, invoice=doc, **r) for r in rows])
        objs = []

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
    if tracked:
        moved = costing.apply_document(org, user, doc, objs, "sale" if kind == SalesInvoice.Kind.INVOICE else "sales_return", date, original)
        if moved:
            inv_acc, cogs_acc = system_account(org, "INVENTORY"), system_account(org, "COGS")
            lines += ([Line(cogs_acc, debit=moved), Line(inv_acc, credit=moved)] if kind == SalesInvoice.Kind.INVOICE
                      else [Line(inv_acc, debit=moved), Line(cogs_acc, credit=moved)])
    txn = post_journal(org, date, lines, f"sales.{kind}", doc.id, doc.number, narration, branch, user)
    doc.journal = txn
    doc.save(update_fields=["journal", "updated_at"])
    for exp, _ in billed:
        exp.billed_invoice = doc
        exp.save(update_fields=["billed_invoice", "updated_at"])
    return doc, True


def _billable_lines(org, party, kind, expenses):
    """Lock and validate the expenses being re-charged. Returns [(expense, line dict)]. Billed at cost including the input VAT
    it carried, with 0% tax: the pass-through is not revenue-with-markup (see docs/ASSUMPTIONS.md)."""
    if not expenses:
        return []
    if kind != SalesInvoice.Kind.INVOICE:
        raise DomainError("Billable expenses can only go on an invoice.", "invalid_billable")
    from apps.expenses.models import Expense

    locked = {e.pk: e for e in Expense.objects.select_for_update().filter(pk__in=[e.pk for e in expenses], organization=org)}
    out = []
    for e in expenses:
        x = locked.get(e.pk)
        if x is None or not x.is_billable or x.status != DocStatus.POSTED or x.customer_id != party.id:
            raise DomainError(f"Expense {e.number} cannot be billed to this customer.", "invalid_billable")
        if x.billed_invoice_id:
            raise DomainError(f"Expense {x.number} has already been billed.", "already_billed")
        text = x.narration or x.reference or x.expense_account.name
        out.append((x, {"description": f"Reimbursable expense {x.number}: {text}"[:300], "quantity": "1", "unit_price": str(x.total), "tax_rate": 0}))
    return out


@transaction.atomic
def void_document(doc, user=None):
    doc = SalesInvoice.objects.select_for_update().get(pk=doc.pk)
    if doc.status == DocStatus.VOID:
        raise DomainError("Document is already void.", "already_void")
    if doc.kind == SalesInvoice.Kind.INVOICE and doc.returns.filter(status=DocStatus.POSTED).exists():
        raise DomainError("Void the sales returns against this invoice first.", "has_returns")
    if doc.kind == "invoice" and doc.receipt_allocations.filter(**{"receipt__status": "posted"}).exists():
        raise DomainError("Void or reduce the receipts/payments allocated to this invoice first.", "has_allocations")
    from apps.inventory import costing
    from apps.treasury import credits

    if doc.kind == "invoice":
        credits.assert_no_credit_links("receipt", doc)
    elif doc.original_id:
        credits.lock_invoices(doc.organization, "receipt", [doc.original_id])
    void_journal(doc.journal, user)
    if doc.items.filter(item__isnull=False).exists():
        costing.reverse_document(doc.organization, user, doc, "sale" if doc.kind == "invoice" else "sales_return", doc.date)
    doc.status = DocStatus.VOID
    doc.save(update_fields=["status", "updated_at"])
    doc.billed_expenses.update(billed_invoice=None)  # re-chargeable again
    if doc.original_id:
        credits.assert_credit_consistent(doc.organization, "receipt", [doc.original_id])
    return doc
