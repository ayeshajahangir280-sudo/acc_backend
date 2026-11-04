from decimal import Decimal

from django.db import transaction

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import DocStatus, next_number
from apps.core.money import money
from apps.ledger.models import AccountType
from apps.ledger.services import Line, post_journal, system_account, void_journal

from .models import Expense

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
RECEIPT_TYPES = {"application/pdf": (b"%PDF",), "image/png": (b"\x89PNG",), "image/jpeg": (b"\xff\xd8\xff",)}


def split_amount(amount, tax_rate, includes_tax):
    """Returns (subtotal, tax, total). With tax-inclusive entry the tax is backed out of the entered amount."""
    amount, rate = money(amount), Decimal(tax_rate or 0)
    if amount <= 0:
        raise DomainError("Amount must be greater than zero.", "invalid_amount")
    if not (0 <= rate <= 100):
        raise DomainError("Tax rate must be between 0 and 100.", "invalid_tax_rate")
    if includes_tax:
        subtotal = money(amount * 100 / (100 + rate))
        return subtotal, amount - subtotal, amount
    tax = money(amount * rate / 100)
    return amount, tax, amount + tax


@retry_on_key_conflict
@transaction.atomic
def create_expense(org, user, *, date, expense_account, paid_through, amount, tax_rate=0, amount_includes_tax=False,
                   vendor=None, reference="", narration="", branch=None, idempotency_key=None):
    existing = find_existing(Expense, org, idempotency_key)
    if existing:
        return existing, False
    if expense_account.organization_id != org.id or paid_through.organization_id != org.id:
        raise DomainError("Account not found.", "invalid_account")
    if expense_account.type != AccountType.EXPENSE or not expense_account.is_active:
        raise DomainError("Choose an active expense account as the category.", "invalid_expense_account")
    if not paid_through.is_cash_bank or not paid_through.is_active:
        raise DomainError("Paid through must be an active cash or bank account.", "invalid_paid_through")
    if vendor is not None and (vendor.organization_id != org.id or vendor.kind not in ("supplier", "both") or not vendor.is_active):
        raise DomainError("Vendor must be an active supplier of this organization.", "invalid_party")
    subtotal, tax, total = split_amount(amount, tax_rate, amount_includes_tax)
    expense = Expense.objects.create(
        organization=org, number=next_number(org, "expense.claim", "EXP"), date=date, branch=branch, narration=narration,
        expense_account=expense_account, paid_through=paid_through, vendor=vendor, reference=reference,
        tax_rate=Decimal(tax_rate or 0), amount_includes_tax=amount_includes_tax, subtotal=subtotal, tax_total=tax, total=total,
        idempotency_key=idempotency_key or None, created_by=user,
    )
    lines = [Line(expense_account, debit=subtotal), Line(paid_through, credit=total)]
    if tax:
        lines.append(Line(system_account(org, "TAX_INPUT"), debit=tax))
    expense.journal = post_journal(org, date, lines, "expenses.expense", expense.id, expense.number, narration, branch, user)
    expense.save(update_fields=["journal", "updated_at"])
    return expense, True


@transaction.atomic
def void_expense(expense, user=None):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status == DocStatus.VOID:
        raise DomainError("Expense is already void.", "already_void")
    void_journal(expense.journal, user)
    expense.status = DocStatus.VOID
    expense.save(update_fields=["status", "updated_at"])
    return expense


def attach_receipt(expense, upload):
    """Store one receipt file (PDF/PNG/JPEG, <= 5 MB). The content signature must match the declared type."""
    if upload.size > MAX_RECEIPT_BYTES:
        raise DomainError("Receipt is larger than 5 MB.", "receipt_too_large")
    signatures = RECEIPT_TYPES.get(upload.content_type)
    head = upload.read(8)
    upload.seek(0)
    if not signatures or not any(head.startswith(s) for s in signatures):
        raise DomainError("Receipt must be a PDF, PNG or JPEG file.", "receipt_invalid_type")
    if expense.receipt:
        expense.receipt.delete(save=False)
    expense.receipt.save(upload.name, upload, save=False)
    expense.save(update_fields=["receipt", "updated_at"])
    return expense
