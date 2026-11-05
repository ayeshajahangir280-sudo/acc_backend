"""Bank rules (suggestions) and the create-from-statement-line shortcut.

create_from_line posts an ordinary expense / receipt / payment through the existing posting services and then matches it to
the statement line, all in one transaction: the ledger entry and the clearing succeed or fail together, and the usual period
lock, reconciled-statement and idempotency rules apply.
"""
from decimal import Decimal

from django.db import transaction

from apps.core.errors import DomainError
from apps.ledger.models import Account, AccountType, JournalLine
from apps.expenses import services as expense_services
from apps.treasury import services as treasury_services

from . import services
from .models import BankRule, BankStatement, StatementLine


def check_rule(org, *, account, category, tax_rate, contains):
    if account is not None and (account.organization_id != org.id or not account.is_cash_bank):
        raise DomainError("A rule can be limited only to a cash or bank account of this organization.", "invalid_account")
    check_category(org, category)
    if not (0 <= Decimal(str(tax_rate or 0)) <= 100):
        raise DomainError("Tax rate must be between 0 and 100.", "invalid_tax_rate")
    if len((contains or "").strip()) < 3:
        raise DomainError("The text to look for must be at least 3 characters.", "rule_too_broad")


def check_category(org, category):
    if category.organization_id != org.id or not category.is_active:
        raise DomainError("Category account not found.", "invalid_account")
    if category.is_cash_bank:
        raise DomainError("Moving money between bank accounts is a transfer, not a categorised transaction.", "invalid_category")
    if (category.system_code or "").startswith("ACCOUNTS_"):
        raise DomainError("Receivable and payable accounts need a customer or supplier; use a receipt or payment instead.", "invalid_category")


def rule_for(line, rules):
    text = f"{line.description} {line.reference}".lower()
    for r in rules:
        if r.account_id not in (None, line.account_id):
            continue
        if r.direction == "in" and line.amount < 0 or r.direction == "out" and line.amount > 0:
            continue
        if r.contains.strip().lower() in text:
            return r
    return None


def suggestions(statement):
    """Unmatched lines with the first matching active rule (if any)."""
    rules = list(BankRule.objects.filter(organization=statement.organization, is_active=True).select_related("category"))
    out = []
    for line in statement.lines.all():
        if services.line_matched(line) != 0:
            continue
        r = rule_for(line, rules)
        out.append({"line": line.id, "date": line.date, "description": line.description, "amount": line.amount,
                    "rule": r.id if r else None, "rule_name": r.name if r else None, "category": r.category_id if r else None,
                    "category_name": r.category.name if r else None, "tax_rate": r.tax_rate if r else Decimal("0")})
    return out


@transaction.atomic
def create_from_line(org, user, statement, *, line_id, category, tax_rate=0, narration=None, branch=None):
    st = BankStatement.objects.select_for_update().get(pk=statement.pk)
    if st.status == BankStatement.Status.RECONCILED:
        raise DomainError("This statement is reconciled. Reopen it to change matches.", "statement_reconciled")
    try:
        line = StatementLine.objects.select_for_update().get(pk=line_id, statement=st, organization=org)
    except StatementLine.DoesNotExist:
        raise DomainError("Statement line not found.", "line_not_found")
    if services.line_matched(line) != 0:
        raise DomainError("This statement line is already (partly) matched. Unmatch it first.", "line_fully_matched")
    check_category(org, category)
    amount = abs(line.amount)
    key = f"bankline-{line.pk}"
    memo = (narration if narration is not None else line.description)[:500]
    if line.amount < 0 and category.type == AccountType.EXPENSE:
        doc, created = expense_services.create_expense(
            org, user, date=line.date, expense_account=category, paid_through=st.account, amount=amount, tax_rate=tax_rate,
            amount_includes_tax=True, reference=line.reference, narration=memo, branch=branch, idempotency_key=key)
    else:
        doc, created = treasury_services.create_cash_document(
            org, user, "receipt" if line.amount > 0 else "payment", date=line.date, amount=amount, cash_account=st.account,
            counter_account=category, method="bank_transfer", reference=line.reference, narration=memo, branch=branch,
            idempotency_key=key, tax_rate=tax_rate)
    if not created:  # the key exists but the line is unmatched: the earlier document was voided; never reuse it silently
        raise DomainError("A document was already created from this line and later voided. Match the line manually instead.", "line_already_posted")
    jl = JournalLine.objects.get(transaction=doc.journal, account=st.account)
    m = services.match(st, user, line_id=line.pk, journal_line_id=jl.pk, method="create")
    return doc, m
