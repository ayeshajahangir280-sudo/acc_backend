"""Customer / supplier statements derived from the AR/AP ledger lines, so the closing balance equals the party's ledger balance."""
from decimal import Decimal

from django.db.models import Sum

from apps.ledger.models import JournalLine

Z = Decimal("0")


def build_statement(org, party, side, date_from, date_to, branch=None):
    """side='sales' -> receivable (debit increases); 'purchases' -> payable (credit increases)."""
    code = "ACCOUNTS_RECEIVABLE" if side == "sales" else "ACCOUNTS_PAYABLE"
    qs = JournalLine.objects.filter(organization=org, party=party, account__system_code=code)
    if branch is not None:
        qs = qs.filter(branch_id__in=[branch] if isinstance(branch, int) else list(branch))
    sign = 1 if side == "sales" else -1
    opening = Z
    if date_from:
        agg = qs.filter(date__lt=date_from).aggregate(d=Sum("debit"), c=Sum("credit"))
        opening = ((agg["d"] or Z) - (agg["c"] or Z)) * sign
    lines = qs.filter(date__lte=date_to)
    if date_from:
        lines = lines.filter(date__gte=date_from)
    rows, bal = [], opening
    for jl in lines.select_related("transaction").order_by("date", "transaction_id", "id"):
        bal += (jl.debit - jl.credit) * sign
        rows.append({"date": jl.date, "reference": jl.transaction.reference, "narration": jl.description or jl.transaction.narration,
                     "debit": jl.debit, "credit": jl.credit, "balance": bal})
    return {"opening": opening, "rows": rows, "closing": bal}
