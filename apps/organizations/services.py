from django.db import transaction

from .models import Membership, Organization


@transaction.atomic
def create_organization(user, **fields):
    from apps.ledger.services import seed_chart_of_accounts

    org = Organization.objects.create(**fields)
    Membership.objects.create(user=user, organization=org, role=Membership.Role.OWNER)
    seed_chart_of_accounts(org)
    return org


def fiscal_year_status(org):
    """Which fiscal year can be closed next, and the closes already recorded."""
    from datetime import timedelta

    from django.utils import timezone

    from .fiscal import fiscal_year_bounds, previous_year_end
    from .models import FiscalYearClose, Organization

    today = timezone.localdate()
    m = org.fiscal_year_start_month
    locked = Organization.objects.filter(pk=org.pk).values_list("books_locked_through", flat=True).first()
    candidate = previous_year_end(today, m)  # the most recent year that has fully ended
    nxt = None
    if locked is not None:
        first_open = locked + timedelta(days=1)
    else:  # nothing locked yet: start from the year of the earliest posted entry
        from apps.ledger.models import JournalTransaction

        first_open = JournalTransaction.objects.filter(organization=org).order_by("date").values_list("date", flat=True).first()
    if first_open is not None:  # the first year end on/after the first open day, so years are closed in order
        end = fiscal_year_bounds(first_open, m)[1]
        nxt = end if end <= candidate else None
    return nxt, list(FiscalYearClose.objects.filter(organization=org))


def close_fiscal_year(org, user, year_end):
    """Lock the books through `year_end` and record the year's profit. No closing journal is posted: profit and loss
    stay readable for the year, and the balance sheet carries the profit forward as prior-year retained earnings."""
    from django.db import transaction
    from django.utils import timezone

    from apps.core.errors import DomainError
    from apps.ledger.models import AccountType, JournalLine
    from django.db.models import Sum

    from .fiscal import fiscal_year_bounds, is_fiscal_year_end
    from .models import FiscalYearClose, Organization

    with transaction.atomic():
        locked_org = Organization.objects.select_for_update().get(pk=org.pk)
        m = locked_org.fiscal_year_start_month
        if not is_fiscal_year_end(year_end, m):
            raise DomainError("That date is not the last day of a fiscal year.", "not_year_end")
        if year_end >= timezone.localdate():
            raise DomainError("The fiscal year has not ended yet.", "year_not_ended")
        if locked_org.books_locked_through is not None and year_end <= locked_org.books_locked_through:
            raise DomainError("That year is already locked.", "already_closed")
        expected, _ = fiscal_year_status(locked_org)
        if expected is None:
            raise DomainError("There is no completed fiscal year with entries to close.", "nothing_to_close")
        if expected != year_end:
            raise DomainError(f"Close the earlier fiscal year first (next to close: {expected}).", "close_in_order")
        lines = JournalLine.objects.filter(organization=locked_org, transaction__date__lte=year_end)
        agg = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        if (agg["d"] or 0) != (agg["c"] or 0):
            raise DomainError("The books do not balance; fix this before closing the year.", "unbalanced_books")
        start = fiscal_year_bounds(year_end, m)[0]
        yr = lines.filter(transaction__date__gte=start)
        def net(kind):
            a = yr.filter(account__type=kind).aggregate(d=Sum("debit"), c=Sum("credit"))
            return (a["c"] or 0) - (a["d"] or 0)
        profit = net(AccountType.INCOME) + net(AccountType.EXPENSE)
        rec = FiscalYearClose.objects.create(organization=locked_org, year_start=start, year_end=year_end, net_profit=profit,
                                             previous_lock=locked_org.books_locked_through, closed_by=user)
        locked_org.books_locked_through = year_end
        locked_org.save(update_fields=["books_locked_through", "updated_at"])
        return rec
