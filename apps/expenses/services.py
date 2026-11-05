from decimal import Decimal

from django.db import transaction

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core import approvals
from apps.core.models import ApprovalStatus, DocStatus, next_number
from apps.core.money import money
from apps.ledger.models import AccountType
from apps.ledger.services import Line, assert_period_open, post_journal, system_account, void_journal
from apps.organizations.access import assert_can_post

import os

from .models import Expense, ExpenseReceipt

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
                   vendor=None, reference="", narration="", branch=None, idempotency_key=None, is_billable=False, customer=None):
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
    if is_billable:
        if customer is None or customer.organization_id != org.id or customer.kind not in ("customer", "both") or not customer.is_active:
            raise DomainError("A billable expense needs an active customer of this organization.", "invalid_customer")
    elif customer is not None:
        raise DomainError("Only billable expenses can name a customer.", "invalid_customer")
    subtotal, tax, total = split_amount(amount, tax_rate, amount_includes_tax)
    pending = approvals.needs_approval(org, "expense", total)
    if pending:  # fail early on things approval cannot fix; the period lock is checked again when it posts
        assert_period_open(org, date)
        assert_can_post(org, user, branch)
    expense = Expense.objects.create(
        organization=org, number=next_number(org, "expense.claim", "EXP"), date=date, branch=branch, narration=narration,
        expense_account=expense_account, paid_through=paid_through, vendor=vendor, reference=reference,
        tax_rate=Decimal(tax_rate or 0), amount_includes_tax=amount_includes_tax, subtotal=subtotal, tax_total=tax, total=total,
        idempotency_key=idempotency_key or None, created_by=user, is_billable=is_billable, customer=customer, status=ApprovalStatus.PENDING if pending else ApprovalStatus.POSTED,
    )
    if not pending:
        _post(expense, user)
    return expense, True


def _post(expense, user):
    org = expense.organization
    lines = [Line(expense.expense_account, debit=expense.subtotal), Line(expense.paid_through, credit=expense.total)]
    if expense.tax_total:
        lines.append(Line(system_account(org, "TAX_INPUT"), debit=expense.tax_total))
    expense.journal = post_journal(org, expense.date, lines, "expenses.expense", expense.id, expense.number, expense.narration, expense.branch, user)
    expense.save(update_fields=["journal", "updated_at"])


@transaction.atomic
def approve_expense(expense, user, membership):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    approvals.check_can_decide(membership, expense)
    approvals.check_self_approval(membership, user, expense)
    _post(expense, user)  # period lock / branch rules apply now; a failure leaves it pending
    approvals.mark_approved(expense, user)
    expense.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return expense


@transaction.atomic
def reject_expense(expense, user, membership, reason):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    approvals.check_can_decide(membership, expense)
    approvals.mark_rejected(expense, user, reason)
    expense.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])
    return expense


@transaction.atomic
def void_expense(expense, user=None):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status == DocStatus.VOID:
        raise DomainError("Expense is already void.", "already_void")
    if expense.status != DocStatus.POSTED:
        raise DomainError("Only posted expenses can be voided; a pending expense can be rejected instead.", "not_posted")
    if expense.billed_invoice_id:
        raise DomainError("This expense has been billed to a customer; void that invoice first.", "expense_billed")
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


MAX_ATTACHMENTS = 5


def _check_upload(upload):
    if upload.size > MAX_RECEIPT_BYTES:
        raise DomainError("Receipt is larger than 5 MB.", "receipt_too_large")
    signatures = RECEIPT_TYPES.get(upload.content_type)
    head = upload.read(8)
    upload.seek(0)
    if not signatures or not any(head.startswith(s) for s in signatures):
        raise DomainError("Receipt must be a PDF, PNG or JPEG file.", "receipt_invalid_type")


@transaction.atomic
def add_attachment(expense, upload, user):
    expense = Expense.objects.select_for_update().get(pk=expense.pk)  # serialises the per-expense file count
    _check_upload(upload)
    if expense.attachments.count() >= MAX_ATTACHMENTS:
        raise DomainError(f"At most {MAX_ATTACHMENTS} receipts per expense.", "too_many_receipts")
    att = ExpenseReceipt(organization=expense.organization, expense=expense, name=os.path.basename(upload.name)[:200],
                         content_type=upload.content_type, size=upload.size, uploaded_by=user)
    att.file.save(upload.name, upload, save=False)
    att.save()
    return att


@transaction.atomic
def remove_attachment(att):
    att.file.delete(save=False)
    att.delete()
