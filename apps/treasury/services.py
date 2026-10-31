from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import DocStatus, next_number
from apps.core.money import money
from apps.ledger.services import Line, post_journal, system_account, void_journal

from .models import Payment, PaymentAllocation, Receipt, ReceiptAllocation
from .selectors import KINDS, outstanding_invoices

# direction -> (model, number prefix, allowed party kinds, control account)
SPEC = {
    "receipt": (Receipt, "RC", ("customer", "both"), "ACCOUNTS_RECEIVABLE"),
    "payment": (Payment, "PV", ("supplier", "both"), "ACCOUNTS_PAYABLE"),
}


@retry_on_key_conflict
@transaction.atomic
def create_cash_document(org, user, direction, *, date, amount, cash_account, party=None, counter_account=None,
                         method="cash", reference="", narration="", branch=None, idempotency_key=None, allocations=()):
    model, prefix, kinds, control_code = SPEC[direction]
    existing = find_existing(model, org, idempotency_key)
    if existing:
        return existing, False
    amount = money(amount)
    if amount <= 0:
        raise DomainError("Amount must be greater than zero.", "invalid_amount")
    if (party is None) == (counter_account is None):
        raise DomainError("Provide exactly one of party or counter_account.", "party_or_account")
    if cash_account.organization_id != org.id or not cash_account.is_cash_bank or not cash_account.is_active:
        raise DomainError("cash_account must be an active cash/bank account of this organization.", "invalid_cash_account")
    if party is not None:
        if party.organization_id != org.id or party.kind not in kinds or not party.is_active:
            raise DomainError(f"Party must be an active {kinds[0]} of this organization.", "invalid_party")
        other = Line(system_account(org, control_code), party=party)
    else:
        if counter_account.organization_id != org.id or not counter_account.is_active or counter_account.id == cash_account.id:
            raise DomainError("Invalid counter account.", "invalid_account")
        other = Line(counter_account)

    doc = model.objects.create(
        organization=org, number=next_number(org, f"treasury.{direction}", prefix), date=date, branch=branch,
        narration=narration, party=party, counter_account=counter_account, cash_account=cash_account, amount=amount,
        method=method, reference=reference, idempotency_key=idempotency_key or None, created_by=user,
    )
    cash = Line(cash_account)
    if direction == "receipt":
        cash.debit, other.credit = amount, amount
    else:
        other.debit, cash.credit = amount, amount
    doc.journal = post_journal(org, date, [cash, other], f"treasury.{direction}", doc.id, doc.number, narration, branch, user)
    doc.save(update_fields=["journal", "updated_at"])
    apply_allocations(org, doc, direction, allocations)
    return doc, True


def apply_allocations(org, doc, direction, allocations):
    """Apply part of `doc` to specific invoices. Must run inside a transaction.

    Rules: party documents only; same party/organization; posted invoices (not returns); amount <= invoice
    outstanding; total allocated <= document amount (remainder stays unapplied). Invoice rows are locked so two
    concurrent receipts cannot over-apply the same invoice.
    """
    if not allocations:
        return
    inv_model, alloc_model, field = KINDS[direction]
    if doc.party_id is None:
        raise DomainError("Allocations require a party document.", "allocation_needs_party")
    amounts = {}
    for a in allocations:
        amt = money(a["amount"])
        if amt <= 0:
            raise DomainError("Allocation amounts must be greater than zero.", "invalid_allocation")
        if a["invoice"].id in amounts:
            raise DomainError("Each invoice may appear only once per request.", "duplicate_allocation")
        amounts[a["invoice"].id] = amt
    doc = type(doc).objects.select_for_update().get(pk=doc.pk)
    if doc.status != DocStatus.POSTED:
        raise DomainError("Cannot allocate a void document.", "void_document")
    already = doc.allocations.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    if already + sum(amounts.values()) > doc.amount:
        raise DomainError(f"Allocations exceed the amount. Unapplied: {doc.amount - already}.", "over_allocated")
    list(inv_model.objects.select_for_update().filter(pk__in=amounts, organization=org).order_by("pk"))  # lock
    found = {i.id: i for i in outstanding_invoices(org, direction, invoice_ids=list(amounts))}
    for inv_id, amt in amounts.items():
        inv = found.get(inv_id)
        if inv is None or inv.party_id != doc.party_id:
            raise DomainError("Invoice must be a posted invoice of the same party.", "invalid_invoice")
        if amt > inv.outstanding:
            raise DomainError(f"{inv.number}: allocation {amt} exceeds outstanding {inv.outstanding}.", "over_applied")
        row, created = alloc_model.objects.get_or_create(organization=org, invoice=inv, **{field: doc}, defaults={"amount": amt})
        if not created:
            row.amount += amt
            row.save(update_fields=["amount", "updated_at"])


@transaction.atomic
def allocate_existing(org, doc, direction, allocations):
    apply_allocations(org, doc, direction, allocations)
    return doc


@transaction.atomic
def void_cash_document(doc, user=None):
    doc = type(doc).objects.select_for_update().get(pk=doc.pk)
    if doc.status == DocStatus.VOID:
        raise DomainError("Document is already void.", "already_void")
    void_journal(doc.journal, user)
    doc.status = DocStatus.VOID
    doc.save(update_fields=["status", "updated_at"])
    return doc
