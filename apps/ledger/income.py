"""Which accounts a sales invoice line may credit, and the organization's default.

Eligible: an active income account of the same organization, excluding the Sales Returns contra account.
"""
from apps.core.errors import DomainError

from .models import Account, AccountType

EXCLUDED_SYSTEM_CODES = {"SALES_RETURNS"}


def is_eligible(org, account):
    return (account is not None and account.organization_id == org.id and account.type == AccountType.INCOME
            and account.is_active and account.system_code not in EXCLUDED_SYSTEM_CODES)


def check_eligible(org, account):
    if not is_eligible(org, account):
        raise DomainError("Choose an active income account of this organization (Sales Returns cannot be used).", "invalid_income_account")
    return account


def default_income_account(org):
    """The organization's configured default if it is still eligible, otherwise the built-in Sales Revenue account."""
    from .services import system_account

    acc = getattr(org, "default_income_account", None)
    if acc is not None and is_eligible(org, acc):
        return acc
    return system_account(org, "SALES")


def eligible_queryset(org):
    return Account.objects.filter(organization=org, type=AccountType.INCOME, is_active=True).exclude(system_code__in=EXCLUDED_SYSTEM_CODES)
