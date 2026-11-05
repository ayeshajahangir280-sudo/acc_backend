"""Recurring expenses: generation, duplicate protection, locks, validation, permissions."""
import threading
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from apps.expenses.models import Expense
from apps.ledger.models import Account
from apps.organizations.models import Branch, BranchAccess, Membership
from apps.parties.models import Party
from apps.recurring import services
from apps.recurring.models import RecurringExpense

from .base import AccountingTestCase, D

User = get_user_model()
URL = "/api/v1/recurring-expenses/"


class RecurringExpenseTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.rent = Account.objects.create(organization=self.bakery, code="6200", name="Rent", type="expense")

    def profile(self, client=None, **kw):
        body = {"name": "Shop rent", "expense_account": self.rent.id, "paid_through": self.acct("CASH").id, "amount": "1000",
                "tax_rate": "10", "amount_includes_tax": False, "frequency": "monthly", "interval": 1, "start_date": "2026-01-31", **kw}
        r = (client or self.client).post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def gen(self, p, on):
        return services.run_expense_profile(p["id"], as_of=date.fromisoformat(on))

    def test_generates_each_due_expense_once_with_correct_posting(self):
        p = self.profile()
        made = self.gen(p, "2026-03-15")
        self.assertEqual([e.date for e in made], [date(2026, 1, 31), date(2026, 2, 28)])
        e = made[0]
        self.assertEqual((e.subtotal, e.tax_total, e.total), (D("1000.00"), D("100.00"), D("1100.00")))
        self.assertEqual(self.balance("CASH"), D("-2200.00"))
        self.assertEqual(self.balance("TAX_INPUT"), D("200.00"))
        self.assertEqual(self.gen(p, "2026-03-15"), [])                  # running again creates nothing
        d = self.client.get(f"{URL}{p['id']}/").data
        self.assertEqual((d["generated"], d["next_date"]), (2, "2026-03-31"))
        self.assertTrue(self.trial_balance_ok())

    def test_tax_inclusive_amount(self):
        p = self.profile(amount="110", amount_includes_tax=True)
        e = self.gen(p, "2026-01-31")[0]
        self.assertEqual((e.subtotal, e.tax_total, e.total), (D("100.00"), D("10.00"), D("110.00")))

    def test_end_date_pause_resume(self):
        p = self.profile(end_date="2026-02-28")
        self.assertEqual(len(self.gen(p, "2026-12-31")), 2)
        self.assertIsNone(self.client.get(f"{URL}{p['id']}/").data["next_date"])
        q = self.profile(name="x")
        self.client.post(f"{URL}{q['id']}/pause/")
        self.assertEqual(self.gen(q, "2026-12-31"), [])
        self.client.post(f"{URL}{q['id']}/resume/")
        self.assertEqual(len(self.gen(q, "2026-02-01")), 1)

    def test_run_now_and_command(self):
        p = self.profile(start_date="2020-01-01", frequency="yearly")
        r = self.client.post(f"{URL}{p['id']}/run-now/")
        self.assertEqual(r.status_code, 200, r.content)
        n = Expense.objects.count()
        self.assertGreaterEqual(n, 6)
        call_command("run_recurring")
        self.assertEqual(Expense.objects.count(), n)

    def test_locked_period_recorded_and_retried(self):
        p = self.profile()
        self.bakery.books_locked_through = date(2026, 2, 28)
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(self.gen(p, "2026-02-15"), [])
        d = self.client.get(f"{URL}{p['id']}/").data
        self.assertIn("period_locked", d["last_error"])
        self.assertEqual(d["generated"], 0)
        self.bakery.books_locked_through = None
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(len(self.gen(p, "2026-02-15")), 1)
        self.assertEqual(self.client.get(f"{URL}{p['id']}/").data["last_error"], "")

    def test_account_deactivated_later_is_reported_not_posted(self):
        p = self.profile()
        self.rent.is_active = False
        self.rent.save()
        self.assertEqual(self.gen(p, "2026-02-15"), [])
        self.assertIn("invalid_expense_account", self.client.get(f"{URL}{p['id']}/").data["last_error"])
        self.assertEqual(Expense.objects.count(), 0)

    def test_voiding_generated_expense_does_not_regenerate(self):
        p = self.profile()
        e = self.gen(p, "2026-01-31")[0]
        self.assertEqual(self.client.post(f"/api/v1/expenses/{e.id}/void/").status_code, 200)
        self.assertEqual(self.gen(p, "2026-01-31"), [])
        self.assertEqual(Expense.objects.count(), 1)

    def test_validation(self):
        base = {"name": "x", "expense_account": self.rent.id, "paid_through": self.acct("CASH").id, "amount": "10",
                "frequency": "monthly", "interval": 1, "start_date": "2026-01-01"}
        cases = [({"amount": "0"}, "invalid_amount"), ({"tax_rate": "150"}, "invalid_tax_rate"), ({"interval": 0}, "invalid_interval"),
                 ({"end_date": "2025-01-01"}, "invalid_due_date"), ({"expense_account": self.acct("CASH").id}, "invalid_expense_account"),
                 ({"paid_through": self.rent.id}, "invalid_paid_through"), ({"vendor": self.customer.id}, "invalid_party")]
        for patch, code in cases:
            r = self.client.post(URL, {**base, **patch}, format="json")
            self.assertEqual(r.status_code, 400, (patch, r.content))
            self.assertEqual(r.data.get("code"), code, (patch, r.content))
        foreign = Account.objects.create(organization=self.other, code="6200", name="R", type="expense")
        r = self.client.post(URL, {**base, "expense_account": foreign.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RecurringExpense.objects.count(), 0)

    def test_schedule_locked_after_generation_but_amount_editable_for_future(self):
        p = self.profile()
        self.gen(p, "2026-01-31")
        r = self.client.patch(f"{URL}{p['id']}/", {"interval": 2}, format="json")
        self.assertEqual(r.data["code"], "schedule_locked")
        self.assertEqual(self.client.patch(f"{URL}{p['id']}/", {"amount": "2000"}, format="json").status_code, 200)
        e = self.gen(p, "2026-02-28")[0]
        self.assertEqual(e.subtotal, D("2000.00"))
        self.assertEqual(Expense.objects.order_by("date").first().subtotal, D("1000.00"))  # earlier expense untouched

    def test_isolation_roles_and_branch_scope(self):
        p = self.profile()
        other = self.as_user(self.bob, self.other)
        self.assertEqual(other.get(f"{URL}{p['id']}/").status_code, 404)
        self.assertEqual(other.post(f"{URL}{p['id']}/run-now/").status_code, 404)
        v = User.objects.create_user("vv", password="pw-12345-xx")
        Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        cv = self.as_user(v, self.bakery)
        self.assertEqual(cv.post(f"{URL}{p['id']}/run-now/").status_code, 403)
        self.assertEqual(cv.get(URL).status_code, 200)
        n, s = (Branch.objects.create(organization=self.bakery, name=x) for x in "NS")
        nora = User.objects.create_user("nora", password="pw-12345-xx")
        m = Membership.objects.create(user=nora, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        BranchAccess.objects.create(membership=m, branch=n)
        cn = self.as_user(nora, self.bakery)
        r = cn.post(URL, {"name": "x", "expense_account": self.rent.id, "paid_through": self.acct("CASH").id, "amount": "5",
                          "frequency": "monthly", "interval": 1, "start_date": "2026-01-01", "branch": s.id}, format="json")
        self.assertEqual(r.data["code"], "branch_forbidden")
        self.assertEqual(cn.get(f"{URL}{p['id']}/").status_code, 404)


class RecurringExpenseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization

        self.user = User.objects.create_user("rex", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race RecExp")

    def test_parallel_runs_create_each_expense_once(self):
        rent = Account.objects.create(organization=self.org, code="6200", name="Rent", type="expense")
        cash = Account.objects.get(organization=self.org, system_code="CASH")
        p = services.create_expense_profile(self.org, self.user, name="x", expense_account=rent, paid_through=cash, amount="10",
                                            frequency="monthly", interval=1, start_date=date(2026, 1, 1))
        barrier = threading.Barrier(4)

        def go():
            try:
                barrier.wait()
                services.run_expense_profile(p.pk, as_of=date(2026, 4, 1))
            finally:
                connection.close()
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(Expense.objects.filter(organization=self.org).count(), 4)
        self.assertEqual(RecurringExpense.objects.get(pk=p.pk).generated, 4)
