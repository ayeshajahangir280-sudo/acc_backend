"""Double-entry posting engine. The ONLY code that writes JournalTransaction / JournalLine."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction

from apps.core.errors import DomainError
from apps.core.models import DocStatus
from apps.core.money import money

from .models import Account, AccountType, JournalLine, JournalTransaction

# (code, name, type, system_code, is_cash_bank). Structure only: no tax rates, no country assumptions.
DEFAULT_CHART = [
    ("1000", "Cash", AccountType.ASSET, "CASH", True),
    ("1100", "Bank", AccountType.ASSET, "BANK", True),
    ("1200", "Accounts Receivable", AccountType.ASSET, "ACCOUNTS_RECEIVABLE", False),
    ("1300", "Input Tax Receivable", AccountType.ASSET, "TAX_INPUT", False),
    ("2000", "Accounts Payable", AccountType.LIABILITY, "ACCOUNTS_PAYABLE", False),
    ("2100", "Output Tax Payable", AccountType.LIABILITY, "TAX_OUTPUT", False),
    ("3000", "Owner's Equity", AccountType.EQUITY, "EQUITY", False),
    ("4000", "Sales Revenue", AccountType.INCOME, "SALES", False),
    ("4100", "Sales Returns", AccountType.INCOME, "SALES_RETURNS", False),
    ("5000", "Purchases", AccountType.EXPENSE, "PURCHASES", False),
    ("5100", "Purchase Returns", AccountType.EXPENSE, "PURCHASE_RETURNS", False),
]


def seed_chart_of_accounts(org):
    Account.objects.bulk_create(
        [Account(organization=org, code=c, name=n, type=t, system_code=s, is_cash_bank=cb) for c, n, t, s, cb in DEFAULT_CHART]
    )


def system_account(org, system_code):
    try:
        return Account.objects.get(organization=org, system_code=system_code)
    except Account.DoesNotExist:
        raise DomainError(f"System account {system_code} is missing for this organization.", "missing_system_account")


@dataclass
class Line:
    account: Account
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    party: Optional[object] = None
    description: str = ""


def _validate(org, lines):
    if len(lines) < 2:
        raise DomainError("A journal entry needs at least two lines.", "too_few_lines")
    total_dr = total_cr = Decimal("0")
    for ln in lines:
        ln.debit, ln.credit = money(ln.debit), money(ln.credit)
        if ln.debit < 0 or ln.credit < 0 or (ln.debit > 0) == (ln.credit > 0):
            raise DomainError("Each line must have exactly one positive side (debit or credit).", "invalid_line")
        if ln.account.organization_id != org.id or not ln.account.is_active:
            raise DomainError("Account does not belong to this organization or is inactive.", "invalid_account")
        if ln.party is not None and ln.party.organization_id != org.id:
            raise DomainError("Party does not belong to this organization.", "invalid_party")
        total_dr += ln.debit
        total_cr += ln.credit
    if total_dr != total_cr:
        raise DomainError(f"Entry is not balanced: debit {total_dr} != credit {total_cr}.", "unbalanced")


def assert_period_open(org, date):
    """Raise DomainError if `date` falls in a locked period (on or before org.books_locked_through).

    Reads the lock from the database rather than the request-scoped `org` object, so a lock set while a request
    is in flight is still honoured. Every posting and voiding path goes through post_journal / void_journal,
    so this is the single enforcement point for all financial documents.
    """
    from apps.organizations.models import Organization

    locked = Organization.objects.filter(pk=org.pk).values_list("books_locked_through", flat=True).first()
    if locked is not None and date <= locked:
        raise DomainError(
            f"The books are locked through {locked.isoformat()}; nothing dated on or before that day can be posted or voided.",
            "period_locked",
        )


@transaction.atomic
def post_journal(org, date, lines, source_type, source_id=None, reference="", narration="", branch=None, user=None):
    _validate(org, lines)
    assert_period_open(org, date)
    if branch is not None and branch.organization_id != org.id:
        raise DomainError("Branch does not belong to this organization.", "invalid_branch")
    from apps.organizations.access import assert_can_post

    assert_can_post(org, user, branch)
    txn = JournalTransaction.objects.create(
        organization=org, date=date, branch=branch, source_type=source_type, source_id=source_id,
        reference=reference, narration=narration, created_by=user,
    )
    JournalLine.objects.bulk_create(
        [
            JournalLine(organization=org, transaction=txn, date=date, branch=branch, account=l.account, party=l.party,
                        debit=l.debit, credit=l.credit, description=l.description)
            for l in lines
        ]
    )
    return txn


@transaction.atomic
def void_journal(txn, user=None):
    """Mark a posted transaction void and post its exact mirror. Idempotent-safe: voiding twice raises."""
    txn = JournalTransaction.objects.select_for_update().get(pk=txn.pk)
    if txn.status == DocStatus.VOID:
        raise DomainError("Already void.", "already_void")
    # The reversal carries the original date, so voiding a locked-period document would rewrite closed books.
    assert_period_open(txn.organization, txn.date)
    from apps.organizations.access import assert_can_void

    assert_can_void(txn.organization, user, txn.branch_id)
    from apps.banking.services import assert_not_reconciled

    assert_not_reconciled(txn)  # a transaction cleared in a reconciled bank statement cannot be voided underneath it
    mirror = JournalTransaction.objects.create(
        organization=txn.organization, date=txn.date, branch=txn.branch, source_type=txn.source_type,
        source_id=txn.source_id, reference=txn.reference, narration=f"Reversal of {txn.reference}".strip(),
        status=DocStatus.VOID, reversal_of=txn, created_by=user,
    )
    JournalLine.objects.bulk_create(
        [
            JournalLine(organization=txn.organization, transaction=mirror, date=l.date, branch=l.branch, account=l.account,
                        party=l.party, debit=l.credit, credit=l.debit, description=f"Reversal: {l.description}"[:300])
            for l in txn.lines.all()
        ]
    )
    txn.status = DocStatus.VOID
    txn.save(update_fields=["status", "updated_at"])
    return mirror
