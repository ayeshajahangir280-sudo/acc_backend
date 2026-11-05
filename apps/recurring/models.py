"""Recurring invoices and bills. A profile is a template plus a schedule; running it creates normal posted documents."""
from django.conf import settings
from django.db import models

from apps.core.models import OrgOwned


class RecurringDocument(OrgOwned):
    class Side(models.TextChoices):
        SALES = "sales", "Sales invoice"
        PURCHASES = "purchases", "Purchase bill"

    class Frequency(models.TextChoices):
        DAILY = "daily", "Day(s)"
        WEEKLY = "weekly", "Week(s)"
        MONTHLY = "monthly", "Month(s)"
        YEARLY = "yearly", "Year(s)"

    side = models.CharField(max_length=10, choices=Side.choices)
    name = models.CharField(max_length=120)
    party = models.ForeignKey("parties.Party", on_delete=models.PROTECT, related_name="+")
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    narration = models.CharField(max_length=500, blank=True)
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    interval = models.PositiveSmallIntegerField(default=1)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    due_days = models.PositiveSmallIntegerField(default=0)       # payment terms: due = document date + due_days
    generated = models.PositiveIntegerField(default=0)           # occurrences created so far; next date = occurrence(generated)
    is_active = models.BooleanField(default=True)
    last_run_on = models.DateField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["organization", "is_active"])]


class RecurringItem(OrgOwned):
    profile = models.ForeignKey(RecurringDocument, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)


class RecurringExpense(OrgOwned):
    """An expense that repeats on a schedule (rent, subscriptions...). Running it creates normal posted expenses."""

    name = models.CharField(max_length=120)
    expense_account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    paid_through = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    amount_includes_tax = models.BooleanField(default=False)
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=100, blank=True)
    narration = models.CharField(max_length=500, blank=True)
    frequency = models.CharField(max_length=10, choices=RecurringDocument.Frequency.choices)
    interval = models.PositiveSmallIntegerField(default=1)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    generated = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    last_run_on = models.DateField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["organization", "is_active"])]
