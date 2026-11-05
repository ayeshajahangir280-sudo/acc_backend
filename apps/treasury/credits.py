"""Credit application: move surplus (credit) on one invoice onto other open invoices of the same party.

A credit exists on an invoice whose outstanding is negative (typically a return posted after the invoice was
paid). Applying it creates NO journal entries: the party's control-account balance is unchanged, only the
invoice-level split moves, so aging/outstanding change while the ledger stays put.

Concurrency/void safety: invoices are row-locked in pk order; voiding a receipt/payment/return re-checks the
invariant "an invoice that has given credit must still have outstanding <= 0" and rolls back if it would break.
"""
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import DocStatus, next_number
from apps.core.money import money
from apps.ledger.services import Line, assert_period_open, post_journal, system_account, void_journal

from .selectors import CREDIT_MODELS, KINDS, REFUND_MODELS, outstanding_invoices

ZERO = Decimal("0")


def _lock(org, direction, ids):
    inv_model = KINDS[direction][0]
    list(inv_model.objects.select_for_update().filter(pk__in=ids, organization=org).order_by("pk"))


def credit_available(row):
    return max(ZERO, -row.outstanding)


def _latest_activity(direction, inv):
    """Latest posted return/receipt/payment date on an invoice: a credit cannot exist before it."""
    _, alloc, doc = KINDS[direction]
    dates = [inv.date]
    r = type(inv).objects.filter(original=inv, status="posted").order_by("-date").values_list("date", flat=True).first()
    a = alloc.objects.filter(invoice=inv, **{f"{doc}__status": "posted"}).order_by(f"-{doc}__date").values_list(f"{doc}__date", flat=True).first()
    return max(d for d in dates + [r, a] if d is not None)


@transaction.atomic
def apply_credit(org, user, direction, from_invoice, applications, date=None):
    """Apply credit held on `from_invoice` to target invoices. `applications` = [{"invoice": obj, "amount": x}]."""
    date = date or timezone.localdate()
    if not applications:
        raise DomainError("Choose at least one invoice to apply the credit to.", "no_applications")
    amounts = {}
    for a in applications:
        amt = money(a["amount"])
        if amt <= 0:
            raise DomainError("Amounts must be greater than zero.", "invalid_amount")
        tid = a["invoice"].id
        if tid == from_invoice.id:
            raise DomainError("An invoice cannot be applied to itself.", "self_application")
        if tid in amounts:
            raise DomainError("Each invoice may appear only once per request.", "duplicate_application")
        amounts[tid] = amt
    if date > timezone.localdate():
        raise DomainError("The application date cannot be in the future.", "future_date")
    assert_period_open(org, date)

    ids = sorted({from_invoice.id, *amounts})
    _lock(org, direction, ids)
    rows = {r.id: r for r in outstanding_invoices(org, direction, invoice_ids=ids)}
    src = rows.get(from_invoice.id)
    if src is None:
        raise DomainError("The credit invoice must be a posted invoice of this organization.", "invalid_invoice")
    available = credit_available(src)
    if sum(amounts.values()) > available:
        raise DomainError(f"Only {available} of credit is available on {src.number}.", "over_applied")
    if date < _latest_activity(direction, src):
        raise DomainError("The application date cannot be before the document's returns/receipts that created the credit.", "date_before_credit")

    model = CREDIT_MODELS[direction]
    for tid, amt in amounts.items():
        tgt = rows.get(tid)
        if tgt is None or tgt.party_id != src.party_id:
            raise DomainError("Target must be a posted invoice of the same party.", "invalid_invoice")
        if tgt.branch_id != src.branch_id:
            raise DomainError(f"{tgt.number}: credit can only be applied within the same branch.", "branch_mismatch")
        if date < tgt.date:
            raise DomainError(f"{tgt.number}: the application date is before the invoice date.", "date_before_invoice")
        if amt > tgt.outstanding:
            raise DomainError(f"{tgt.number}: {amt} exceeds outstanding {tgt.outstanding}.", "over_applied")
        row, created = model.objects.get_or_create(organization=org, from_invoice_id=src.id, to_invoice_id=tid,
                                                   defaults={"amount": amt, "date": date, "applied_by": user})
        if not created:
            row.amount += amt
            row.date = max(row.date, date)
            row.save(update_fields=["amount", "date", "updated_at"])
    return credit_summary(org, direction, src.id)


@transaction.atomic
def unapply_credit(org, direction, application_id):
    """Remove one credit application (restores the credit on the source and the balance on the target)."""
    model = CREDIT_MODELS[direction]
    row = model.objects.filter(organization=org, pk=application_id).first()
    if row is None:
        raise DomainError("Credit application not found.", "not_found")
    _lock(org, direction, sorted({row.from_invoice_id, row.to_invoice_id}))
    row = model.objects.filter(organization=org, pk=application_id).first()  # re-read under the lock
    if row is None:
        raise DomainError("Credit application not found.", "not_found")
    assert_period_open(org, row.date)  # undoing it would change settled history
    src = row.from_invoice_id
    row.delete()
    return credit_summary(org, direction, src)


def credit_summary(org, direction, invoice_id):
    model = CREDIT_MODELS[direction]
    row = outstanding_invoices(org, direction, invoice_ids=[invoice_id]).first()
    if row is None:
        raise DomainError("Invoice not found.", "not_found")

    def ser(qs, other):
        return [{"id": a.id, "invoice": getattr(a, other).id, "number": getattr(a, other).number, "amount": a.amount, "date": a.date}
                for a in qs.select_related(other).order_by("id")]

    return {
        "invoice": row.id, "number": row.number, "total": row.total, "outstanding": row.outstanding,
        "credit_available": credit_available(row),
        "credit_applied": row.applied_out, "credit_received": row.applied_in,
        "given": ser(model.objects.filter(from_invoice_id=invoice_id), "to_invoice"),
        "received": ser(model.objects.filter(to_invoice_id=invoice_id), "from_invoice"),
    }


def credit_invoices(org, direction, party=None, branch=None):
    """Invoices that currently hold unapplied credit."""
    return outstanding_invoices(org, direction, party=party, branch=branch).filter(outstanding__lt=0).order_by("date", "id")


# ---- void guards -------------------------------------------------------------------------------------------

def lock_invoices(org, direction, ids):
    if ids:
        _lock(org, direction, sorted(set(ids)))


def assert_credit_consistent(org, direction, ids):
    """After a void: an invoice that has given credit away must still hold at least that much surplus."""
    if not ids:
        return
    model = CREDIT_MODELS[direction]
    given = set(model.objects.filter(from_invoice_id__in=ids).values_list("from_invoice_id", flat=True))
    given |= set(REFUND_MODELS[direction].objects.filter(invoice_id__in=ids, status="posted").values_list("invoice_id", flat=True))
    if not given:
        return
    bad = [r for r in outstanding_invoices(org, direction, invoice_ids=list(given)) if r.outstanding > 0]
    if bad:
        raise DomainError(
            f"{bad[0].number} has credit applied to other invoices or refunded; remove those credit applications / void those refunds before voiding this document.",
            "credit_applied",
        )


def assert_no_credit_links(direction, inv):
    model = CREDIT_MODELS[direction]
    if model.objects.filter(from_invoice=inv).exists() or model.objects.filter(to_invoice=inv).exists():
        raise DomainError("Remove the credit applications on this invoice first.", "has_credit_applications")
    if REFUND_MODELS[direction].objects.filter(invoice=inv, status="posted").exists():
        raise DomainError("Void the credit refunds on this invoice first.", "has_refunds")


# ---- refunds -----------------------------------------------------------------------------------------------

@retry_on_key_conflict
@transaction.atomic
def refund_credit(org, user, direction, invoice, *, cash_account, amount, date=None, method="cash", reference="", narration="",
                  idempotency_key=None):
    """Pay back (sales) or collect back (purchases) credit held on `invoice`. Posts a journal; never exceeds the credit available.
    Returns (refund, created)."""
    model = REFUND_MODELS[direction]
    existing = find_existing(model, org, idempotency_key)
    if existing:
        return existing, False
    date = date or timezone.localdate()
    amount = money(amount)
    if amount <= 0:
        raise DomainError("Amount must be greater than zero.", "invalid_amount")
    if date > timezone.localdate():
        raise DomainError("The refund date cannot be in the future.", "future_date")
    if cash_account.organization_id != org.id or not cash_account.is_cash_bank or not cash_account.is_active:
        raise DomainError("Choose an active cash or bank account.", "invalid_cash_account")
    assert_period_open(org, date)
    _lock(org, direction, [invoice.id])
    row = outstanding_invoices(org, direction, invoice_ids=[invoice.id]).first()
    if row is None:
        raise DomainError("The credit invoice must be a posted invoice of this organization.", "invalid_invoice")
    available = credit_available(row)
    if amount > available:
        raise DomainError(f"Only {available} of credit is available on {row.number}.", "over_refund")
    if date < _latest_activity(direction, row):
        raise DomainError("The refund date cannot be before the returns/receipts that created the credit.", "date_before_credit")
    party = row.party
    prefix = "CRF" if direction == "receipt" else "SRF"
    refund = model.objects.create(
        organization=org, number=next_number(org, f"refund.{direction}", prefix), date=date, branch=row.branch, narration=narration,
        invoice=row, party=party, cash_account=cash_account, amount=amount, method=method, reference=reference,
        idempotency_key=idempotency_key or None, created_by=user)
    if direction == "receipt":
        lines = [Line(system_account(org, "ACCOUNTS_RECEIVABLE"), debit=amount, party=party), Line(cash_account, credit=amount)]
    else:
        lines = [Line(cash_account, debit=amount), Line(system_account(org, "ACCOUNTS_PAYABLE"), credit=amount, party=party)]
    refund.journal = post_journal(org, date, lines, f"treasury.refund_{direction}", refund.id, refund.number,
                                  narration or f"Credit refund {row.number}", row.branch, user)
    refund.save(update_fields=["journal", "updated_at"])
    return refund, True


@transaction.atomic
def void_refund(org, user, direction, refund_id):
    model = REFUND_MODELS[direction]
    refund = model.objects.select_for_update().filter(organization=org, pk=refund_id).first()
    if refund is None:
        raise DomainError("Refund not found.", "not_found")
    if refund.status == DocStatus.VOID:
        raise DomainError("Refund is already void.", "already_void")
    _lock(org, direction, [refund.invoice_id])
    void_journal(refund.journal, user)  # period lock, branch scope and the reconciled-bank guard apply here
    refund.status = DocStatus.VOID
    refund.save(update_fields=["status", "updated_at"])
    return refund
