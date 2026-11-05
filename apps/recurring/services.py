import calendar
from datetime import date as date_type, timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.errors import DomainError
from apps.purchases import services as purchase_services
from apps.sales import services as sales_services

from .models import RecurringDocument, RecurringExpense, RecurringItem

MAX_PER_RUN = 31  # catch-up cap per profile per run


def occurrence(profile, k):
    """k-th scheduled date, computed from the start date (so a 31st does not drift to the 28th)."""
    n = profile.interval * k
    s = profile.start_date
    if profile.frequency == "daily":
        return s + timedelta(days=n)
    if profile.frequency == "weekly":
        return s + timedelta(weeks=n)
    months = n if profile.frequency == "monthly" else 12 * n
    y, m = divmod(s.month - 1 + months, 12)
    y, m = s.year + y, m + 1
    return date_type(y, m, min(s.day, calendar.monthrange(y, m)[1]))


def next_date(profile):
    d = occurrence(profile, profile.generated)
    return None if profile.end_date and d > profile.end_date else d


def _check(org, side, party, branch):
    kinds = ("customer", "both") if side == "sales" else ("supplier", "both")
    if party.organization_id != org.id or party.kind not in kinds or not party.is_active:
        raise DomainError(f"Party must be an active {kinds[0]} of this organization.", "invalid_party")
    if branch is not None and (branch.organization_id != org.id or not branch.is_active):
        raise DomainError("Branch must be an active branch of this organization.", "invalid_branch")


def _items(org, profile, items):
    svc = sales_services if profile.side == "sales" else purchase_services
    svc._prepare_items(items)  # validates quantities/prices/rates and non-zero total
    profile.items.all().delete()
    RecurringItem.objects.bulk_create([
        RecurringItem(organization=org, profile=profile, description=i["description"], quantity=i["quantity"],
                      unit_price=i["unit_price"], tax_rate=i.get("tax_rate") or 0) for i in items])


@transaction.atomic
def create_profile(org, user, *, side, items, party, branch=None, **fields):
    from apps.organizations.access import assert_can_post

    _check(org, side, party, branch)
    assert_can_post(org, user, branch)
    if fields["interval"] < 1:
        raise DomainError("Repeat interval must be at least 1.", "invalid_interval")
    if fields.get("end_date") and fields["end_date"] < fields["start_date"]:
        raise DomainError("End date cannot be before the start date.", "invalid_due_date")
    p = RecurringDocument.objects.create(organization=org, side=side, party=party, branch=branch, created_by=user, **fields)
    _items(org, p, items)
    return p


@transaction.atomic
def update_profile(org, profile, *, items=None, **fields):
    profile = RecurringDocument.objects.select_for_update().get(pk=profile.pk)
    for k in ("frequency", "interval", "start_date"):
        if k in fields and fields[k] != getattr(profile, k) and profile.generated:
            raise DomainError("The schedule cannot change after documents were generated; create a new profile.", "schedule_locked")
    for k, v in fields.items():
        setattr(profile, k, v)
    if profile.end_date and profile.end_date < profile.start_date:
        raise DomainError("End date cannot be before the start date.", "invalid_due_date")
    profile.save()
    if items is not None:
        _items(org, profile, items)
    return profile


@transaction.atomic
def set_active(profile, active):
    profile = RecurringDocument.objects.select_for_update().get(pk=profile.pk)
    profile.is_active = active
    profile.save(update_fields=["is_active", "updated_at"])
    return profile


def run_profile(profile_id, as_of=None):
    """Create every due occurrence (up to the cap). Each document is its own transaction with a per-occurrence
    idempotency key, so concurrent or repeated runs can never create the same occurrence twice. Stops at the first
    failure (locked period, inactive branch...) and records it; later runs retry from that occurrence."""
    as_of = as_of or timezone.localdate()
    made = []
    for _ in range(MAX_PER_RUN):
        try:
            with transaction.atomic():
                p = RecurringDocument.objects.select_for_update(of=("self",)).select_related("party", "branch", "organization").get(pk=profile_id)
                if not p.is_active:
                    break
                d = next_date(p)
                if d is None or d > as_of:
                    break
                if p.created_by is None:
                    raise DomainError("The profile has no owner to post as; recreate it.", "no_owner")
                svc = sales_services if p.side == "sales" else purchase_services
                items = [{"description": i.description, "quantity": i.quantity, "unit_price": i.unit_price, "tax_rate": i.tax_rate}
                         for i in p.items.all()]
                doc, _ = svc.create_document(
                    p.organization, p.created_by, kind="invoice", party=p.party, date=d, items=items, branch=p.branch,
                    narration=p.narration or p.name, due_date=d + timedelta(days=p.due_days),
                    idempotency_key=f"recurring-{p.pk}-{p.generated}")
                p.generated += 1
                p.last_run_on, p.last_error = as_of, ""
                p.save(update_fields=["generated", "last_run_on", "last_error", "updated_at"])
                made.append(doc)
        except DomainError as e:
            RecurringDocument.objects.filter(pk=profile_id).update(last_error=f"{e.code}: {e}"[:300])
            break
    return made


def run_due(org=None, as_of=None):
    """Run all active profiles (one organization, or all). Returns {profile_id: [documents]}."""
    qs = RecurringDocument.objects.filter(is_active=True)
    if org is not None:
        qs = qs.filter(organization=org)
    return {pid: run_profile(pid, as_of) for pid in qs.values_list("pk", flat=True)}


# ---- recurring expenses -------------------------------------------------------------------------------------------

def _check_expense(org, user, *, expense_account, paid_through, vendor, branch, amount, tax_rate, amount_includes_tax):
    """Same rules as a manual expense, checked when the profile is saved so mistakes show up immediately."""
    from apps.expenses.services import split_amount
    from apps.ledger.models import AccountType
    from apps.organizations.access import assert_can_post

    if expense_account.organization_id != org.id or paid_through.organization_id != org.id:
        raise DomainError("Account not found.", "invalid_account")
    if expense_account.type != AccountType.EXPENSE or not expense_account.is_active:
        raise DomainError("Choose an active expense account as the category.", "invalid_expense_account")
    if not paid_through.is_cash_bank or not paid_through.is_active:
        raise DomainError("Paid through must be an active cash or bank account.", "invalid_paid_through")
    if vendor is not None and (vendor.organization_id != org.id or vendor.kind not in ("supplier", "both") or not vendor.is_active):
        raise DomainError("Vendor must be an active supplier of this organization.", "invalid_party")
    if branch is not None and (branch.organization_id != org.id or not branch.is_active):
        raise DomainError("Branch must be an active branch of this organization.", "invalid_branch")
    assert_can_post(org, user, branch)
    split_amount(amount, tax_rate, amount_includes_tax)  # amount > 0, tax rate in range


@transaction.atomic
def create_expense_profile(org, user, **f):
    if f["interval"] < 1:
        raise DomainError("Repeat interval must be at least 1.", "invalid_interval")
    if f.get("end_date") and f["end_date"] < f["start_date"]:
        raise DomainError("End date cannot be before the start date.", "invalid_due_date")
    _check_expense(org, user, expense_account=f["expense_account"], paid_through=f["paid_through"], vendor=f.get("vendor"),
                   branch=f.get("branch"), amount=f["amount"], tax_rate=f.get("tax_rate") or 0,
                   amount_includes_tax=f.get("amount_includes_tax", False))
    return RecurringExpense.objects.create(organization=org, created_by=user, **f)


@transaction.atomic
def update_expense_profile(org, user, profile, **fields):
    profile = RecurringExpense.objects.select_for_update().get(pk=profile.pk)
    for k in ("frequency", "interval", "start_date"):
        if k in fields and fields[k] != getattr(profile, k) and profile.generated:
            raise DomainError("The schedule cannot change after expenses were generated; create a new profile.", "schedule_locked")
    for k, v in fields.items():
        setattr(profile, k, v)
    if profile.end_date and profile.end_date < profile.start_date:
        raise DomainError("End date cannot be before the start date.", "invalid_due_date")
    _check_expense(org, user, expense_account=profile.expense_account, paid_through=profile.paid_through, vendor=profile.vendor,
                   branch=profile.branch, amount=profile.amount, tax_rate=profile.tax_rate, amount_includes_tax=profile.amount_includes_tax)
    profile.save()
    return profile


@transaction.atomic
def set_expense_active(profile, active):
    profile = RecurringExpense.objects.select_for_update().get(pk=profile.pk)
    profile.is_active = active
    profile.save(update_fields=["is_active", "updated_at"])
    return profile


def run_expense_profile(profile_id, as_of=None):
    """Create every due expense (up to the cap). Same safety model as run_profile: one transaction and one
    idempotency key per occurrence, stop at the first failure and record it."""
    from apps.expenses import services as expense_services

    as_of = as_of or timezone.localdate()
    made = []
    for _ in range(MAX_PER_RUN):
        try:
            with transaction.atomic():
                p = RecurringExpense.objects.select_for_update(of=("self",)).select_related(
                    "organization", "expense_account", "paid_through", "vendor", "branch").get(pk=profile_id)
                if not p.is_active:
                    break
                d = next_date(p)
                if d is None or d > as_of:
                    break
                if p.created_by is None:
                    raise DomainError("The profile has no owner to post as; recreate it.", "no_owner")
                exp, _ = expense_services.create_expense(
                    p.organization, p.created_by, date=d, expense_account=p.expense_account, paid_through=p.paid_through,
                    amount=p.amount, tax_rate=p.tax_rate, amount_includes_tax=p.amount_includes_tax, vendor=p.vendor,
                    reference=p.reference, narration=p.narration or p.name, branch=p.branch,
                    idempotency_key=f"recurring-exp-{p.pk}-{p.generated}")
                p.generated += 1
                p.last_run_on, p.last_error = as_of, ""
                p.save(update_fields=["generated", "last_run_on", "last_error", "updated_at"])
                made.append(exp)
        except DomainError as e:
            RecurringExpense.objects.filter(pk=profile_id).update(last_error=f"{e.code}: {e}"[:300])
            break
    return made


def run_due_expenses(org=None, as_of=None):
    qs = RecurringExpense.objects.filter(is_active=True)
    if org is not None:
        qs = qs.filter(organization=org)
    return {pid: run_expense_profile(pid, as_of) for pid in qs.values_list("pk", flat=True)}
