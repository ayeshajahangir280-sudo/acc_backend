"""Recurring invoices and bills: schedule maths, generation, duplicate protection, locks and permissions."""
import threading
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from apps.organizations.models import Branch, BranchAccess, Membership
from apps.parties.models import Party
from apps.purchases.models import PurchaseInvoice
from apps.recurring import services
from apps.recurring.models import RecurringDocument
from apps.sales.models import SalesInvoice

from .base import AccountingTestCase, D

User = get_user_model()
URL = "/api/v1/recurring/"


class RecurringBase(AccountingTestCase):
    def profile(self, client=None, side="sales", **kw):
        party = self.customer if side == "sales" else self.supplier
        body = {"side": side, "name": "Rent", "party": party.id, "frequency": "monthly", "interval": 1, "start_date": "2026-01-31",
                "due_days": 15, "items": [{"description": "Service", "quantity": "1", "unit_price": "100", "tax_rate": "10"}], **kw}
        r = (client or self.client).post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def gen(self, p, on):
        obj = RecurringDocument.objects.get(pk=p["id"])
        return services.run_profile(obj.pk, as_of=date.fromisoformat(on))


class ScheduleTests(RecurringBase):
    def test_monthly_month_end_does_not_drift(self):
        p = RecurringDocument(frequency="monthly", interval=1, start_date=date(2026, 1, 31))
        self.assertEqual([services.occurrence(p, k) for k in range(4)], [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_other_frequencies(self):
        mk = lambda f, i: RecurringDocument(frequency=f, interval=i, start_date=date(2026, 3, 1))
        self.assertEqual(services.occurrence(mk("daily", 10), 2), date(2026, 3, 21))
        self.assertEqual(services.occurrence(mk("weekly", 2), 1), date(2026, 3, 15))
        self.assertEqual(services.occurrence(mk("yearly", 1), 2), date(2028, 3, 1))
        self.assertEqual(services.occurrence(mk("monthly", 3), 1), date(2026, 6, 1))


class GenerationTests(RecurringBase):
    def test_generates_missed_occurrences_once_each(self):
        p = self.profile()
        made = self.gen(p, "2026-03-15")
        self.assertEqual([str(d.date) for d in made], ["2026-01-31", "2026-02-28"])
        self.assertEqual(SalesInvoice.objects.count(), 2)
        inv = SalesInvoice.objects.order_by("date").first()
        self.assertEqual((inv.total, str(inv.due_date), inv.status), (D("110.00"), "2026-02-15", "posted"))
        self.assertEqual(self.gen(p, "2026-03-15"), [])           # nothing more due
        self.assertEqual(SalesInvoice.objects.count(), 2)
        self.assertEqual(len(self.gen(p, "2026-03-31")), 1)       # picks up the next one
        self.assertTrue(self.trial_balance_ok())
        d = self.client.get(f"{URL}{p['id']}/").data
        self.assertEqual((d["generated"], d["next_date"]), (3, "2026-04-30"))

    def test_end_date_stops_generation(self):
        p = self.profile(end_date="2026-02-28")
        self.assertEqual(len(self.gen(p, "2026-12-31")), 2)
        self.assertIsNone(self.client.get(f"{URL}{p['id']}/").data["next_date"])

    def test_purchase_profile_makes_bills(self):
        p = self.profile(side="purchases")
        self.gen(p, "2026-02-01")
        self.assertEqual(PurchaseInvoice.objects.filter(kind="invoice").count(), 1)
        self.assertEqual(SalesInvoice.objects.count(), 0)

    def test_pause_and_resume(self):
        p = self.profile()
        self.assertFalse(self.client.post(f"{URL}{p['id']}/pause/").data["is_active"])
        self.assertEqual(self.gen(p, "2026-06-01"), [])
        self.client.post(f"{URL}{p['id']}/resume/")
        self.assertEqual(len(self.gen(p, "2026-02-01")), 2 if False else 1)

    def test_run_now_endpoint_and_command(self):
        p = self.profile(start_date="2020-01-01", frequency="yearly")
        r = self.client.post(f"{URL}{p['id']}/run-now/")
        self.assertEqual(r.status_code, 200, r.content)
        n = SalesInvoice.objects.count()
        self.assertGreaterEqual(n, 6)
        call_command("run_recurring")
        self.assertEqual(SalesInvoice.objects.count(), n)           # command is a no-op when nothing is due

    def test_locked_period_records_error_and_retries_later(self):
        p = self.profile()
        self.bakery.books_locked_through = date(2026, 2, 28)
        self.bakery.save()
        self.assertEqual(self.gen(p, "2026-02-15"), [])
        d = self.client.get(f"{URL}{p['id']}/").data
        self.assertIn("period_locked", d["last_error"])
        self.assertEqual(d["generated"], 0)
        self.bakery.books_locked_through = None
        self.bakery.save()
        self.assertEqual(len(self.gen(p, "2026-02-15")), 1)
        self.assertEqual(self.client.get(f"{URL}{p['id']}/").data["last_error"], "")

    def test_inactive_branch_is_reported_not_posted(self):
        b = Branch.objects.create(organization=self.bakery, name="B")
        p = self.profile(branch=b.id)
        b.is_active = False
        b.save()
        self.assertEqual(self.gen(p, "2026-02-15"), [])
        self.assertIn("inactive_branch", self.client.get(f"{URL}{p['id']}/").data["last_error"])

    def test_voiding_a_generated_invoice_does_not_regenerate_it(self):
        p = self.profile()
        made = self.gen(p, "2026-02-01")
        self.client.post(f"/api/v1/sales/invoices/{made[0].id}/void/")
        self.assertEqual(self.gen(p, "2026-02-01"), [])
        self.assertEqual(SalesInvoice.objects.count(), 1)

    def test_schedule_change_blocked_after_generation_but_items_editable(self):
        p = self.profile()
        self.gen(p, "2026-02-01")
        r = self.client.patch(f"{URL}{p['id']}/", {"frequency": "weekly"}, format="json")
        self.assertEqual(r.data["code"], "schedule_locked")
        r = self.client.patch(f"{URL}{p['id']}/", {"items": [{"description": "New", "quantity": "2", "unit_price": "50", "tax_rate": "0"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        made = self.gen(p, "2026-03-05")
        self.assertEqual(made[0].total, D("100.00"))
        self.assertEqual(SalesInvoice.objects.order_by("date").first().total, D("110.00"))   # earlier invoice unchanged

    def test_validation(self):
        bad = lambda **k: self.client.post(URL, {"side": "sales", "name": "x", "party": self.customer.id, "frequency": "monthly", "interval": 1,
                                                 "start_date": "2026-01-01", "items": [{"description": "a", "quantity": "1", "unit_price": "1"}], **k}, format="json")
        self.assertEqual(bad(party=self.supplier.id).data["code"], "invalid_party")
        self.assertEqual(bad(interval=0).data["code"], "invalid_interval")
        self.assertEqual(bad(end_date="2025-01-01").data["code"], "invalid_due_date")
        self.assertEqual(bad(items=[]).status_code, 400)

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
        self.assertEqual(cn.post(URL, {"side": "sales", "name": "x", "party": self.customer.id, "frequency": "monthly", "interval": 1, "start_date": "2026-01-01",
                                       "branch": s.id, "items": [{"description": "a", "quantity": "1", "unit_price": "1"}]}, format="json").data["code"], "branch_forbidden")
        self.assertEqual(cn.get(f"{URL}{p['id']}/").status_code, 404)    # unbranched profile hidden from a restricted user


class RecurringConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization

        self.user = User.objects.create_user("rec", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race Rec")
        self.party = Party.objects.create(organization=self.org, name="C", kind="customer")

    def test_parallel_runs_create_each_occurrence_once(self):
        p = services.create_profile(self.org, self.user, side="sales", party=self.party, name="x", frequency="monthly", interval=1,
                                    start_date=date(2026, 1, 1), items=[{"description": "a", "quantity": "1", "unit_price": "10"}])
        barrier = threading.Barrier(4)

        def go():
            try:
                barrier.wait()
                services.run_profile(p.pk, as_of=date(2026, 4, 1))
            finally:
                connection.close()
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(SalesInvoice.objects.filter(organization=self.org).count(), 4)
        self.assertEqual(RecurringDocument.objects.get(pk=p.pk).generated, 4)
