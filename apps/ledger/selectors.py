"""Read-side queries over the ledger. Single aggregate queries; no per-row lookups."""
from decimal import Decimal

from django.db.models import Q, Sum

from .models import JournalLine


def party_balances(org, party_id=None, as_of=None):
    """Returns {party_id: {"receivable": Decimal, "payable": Decimal}} from AR/AP ledger lines.

    receivable = debit - credit on Accounts Receivable; payable = credit - debit on Accounts Payable.
    """
    qs = JournalLine.objects.filter(
        organization=org, party__isnull=False, account__system_code__in=["ACCOUNTS_RECEIVABLE", "ACCOUNTS_PAYABLE"]
    )
    if party_id:
        qs = qs.filter(party_id=party_id)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    rows = qs.values("party_id").annotate(
        ar_dr=Sum("debit", filter=Q(account__system_code="ACCOUNTS_RECEIVABLE")),
        ar_cr=Sum("credit", filter=Q(account__system_code="ACCOUNTS_RECEIVABLE")),
        ap_dr=Sum("debit", filter=Q(account__system_code="ACCOUNTS_PAYABLE")),
        ap_cr=Sum("credit", filter=Q(account__system_code="ACCOUNTS_PAYABLE")),
    )
    z = Decimal("0")
    return {
        r["party_id"]: {
            "receivable": (r["ar_dr"] or z) - (r["ar_cr"] or z),
            "payable": (r["ap_cr"] or z) - (r["ap_dr"] or z),
        }
        for r in rows
    }


def account_balances(org, as_of=None):
    qs = JournalLine.objects.filter(organization=org)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    return {
        r["account_id"]: (r["dr"] or Decimal("0"), r["cr"] or Decimal("0"))
        for r in qs.values("account_id").annotate(dr=Sum("debit"), cr=Sum("credit"))
    }
