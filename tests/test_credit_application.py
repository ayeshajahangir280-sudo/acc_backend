"""Credit application: customer/supplier credits applied to open invoices without touching the ledger."""
import threading
from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.ledger.models import JournalLine, JournalTransaction
from apps.organizations.models import Branch, BranchAccess, Membership
from apps.parties.models import Party
from apps.treasury.models import PurchaseCreditApplication, SalesCreditApplication

from .base import AccountingTestCase, D
from .test_branch_access import BranchBase

User = get_user_model()
SALES, PURCH = "/api/v1/sales/invoices/", "/api/v1/purchases/invoices/"


class CreditBase(AccountingTestCase):
    """Side-parameterised helpers. Invoice totals: price 100 + 10% tax = 110 per unit."""

    side = "sales"

    @property
    def base(self):
        return SALES if self.side == "sales" else PURCH

    @property
    def party(self):
        return self.customer if self.side == "sales" else self.supplier

    @property
    def cash_url(self):
        return "/api/v1/receipts/" if self.side == "sales" else "/api/v1/payments/"

    def doc(self, price="100", on="2026-01-10", client=None, branch=None, party=None, **extra):
        body = {"party": (party or self.party).id, "date": on, "branch": branch,
                "items": [{"description": "x", "quantity": "1", "unit_price": price, "tax_rate": "10"}], **extra}
        r = (client or self.client).post(self.base, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def ret(self, original, price="100", on="2026-01-20", client=None, **kw):
        return self.doc(price, on, client=client, kind="return", original=original["id"], branch=original.get("branch"), **kw)

    def pay(self, invoice, amount, on="2026-01-15", client=None):
        body = {"party": self.party.id, "cash_account": self.acct("BANK").id, "amount": amount, "date": on,
                "branch": invoice.get("branch"), "allocations": [{"invoice": invoice["id"], "amount": amount}]}
        r = (client or self.client).post(self.cash_url, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def credit_invoice(self, total_returned="110", **kw):
        """A fully-paid 110 invoice with a return of `total_returned` after payment -> credit available."""
        inv = self.doc(**kw)
        self.pay(inv, "110")
        self.ret(inv, price=str(D(total_returned) / D("1.1")))
        return inv

    def apply(self, src, targets, client=None, **kw):
        body = {"applications": [{"invoice": t["id"], "amount": a} for t, a in targets], **kw}
        return (client or self.client).post(f"{self.base}{src['id']}/apply-credit/", body, format="json")

    def summary(self, inv, client=None):
        return (client or self.client).get(f"{self.base}{inv['id']}/credit/").data

    def outstanding(self, inv):
        rows = (self.client.get(f"{self.cash_url}open-invoices/?party={self.party.id}").data)
        return next((D(r["outstanding"]) for r in rows if r["id"] == inv["id"]), None)

    def aging_row_outstanding(self, snap):
        return sum(D(str(r["outstanding"])) for r in snap["rows"])

    @property
    def cmodel(self):
        return SalesCreditApplication if self.side == "sales" else PurchaseCreditApplication

    def ledger_state(self):
        return (JournalTransaction.objects.count(), JournalLine.objects.count())

    def aging_total(self):
        r = self.client.get("/api/v1/reports/aging/", {"type": "receivable" if self.side == "sales" else "payable", "as_of": "2026-12-31"})
        self.assertEqual(r.status_code, 200, r.content)
        return r.data


class CreditApplicationTests(CreditBase):
    def test_partial_then_full_application_shows_remaining_credit(self):
        src = self.credit_invoice()                       # 110 paid, 110 returned -> 110 credit
        t1, t2 = self.doc(price="100"), self.doc(price="200")   # 110, 220 open
        before = self.ledger_state()
        r = self.apply(src, [(t1, "40")])
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(D(r.data["credit_available"]), D("70"))
        self.assertEqual(D(r.data["credit_applied"]), D("40"))
        self.assertEqual(self.outstanding(t1), D("70"))
        r = self.apply(src, [(t1, "70"), (t2, "0.01")])
        self.assertEqual(r.status_code, 400, r.content)    # 70.01 > 70 available
        self.assertEqual(r.data["code"], "over_applied")
        self.assertEqual(self.apply(src, [(t1, "70")]).status_code, 200)
        s = self.summary(src)
        self.assertEqual(D(s["credit_available"]), D("0"))
        self.assertEqual(len(s["given"]), 1)                # same pair accumulates into one row
        self.assertEqual(D(s["given"][0]["amount"]), D("110"))
        self.assertIsNone(self.outstanding(t1))             # fully settled -> not open any more
        self.assertEqual(self.ledger_state(), before)       # no ledger entries at all
        self.assertTrue(self.trial_balance_ok())

    def test_cannot_exceed_target_outstanding_or_apply_to_self_or_duplicate(self):
        src = self.credit_invoice()
        small = self.doc(price="10")                         # 11 outstanding
        self.assertEqual(self.apply(src, [(small, "11.01")]).data["code"], "over_applied")
        self.assertEqual(self.apply(src, [(src, "1")]).data["code"], "self_application")
        self.assertEqual(self.apply(src, [(small, "1"), (small, "1")]).data["code"], "duplicate_application")
        self.assertEqual(self.apply(src, [(small, "0")]).data["code"], "invalid_amount")
        self.assertEqual(self.cmodel.objects.count(), 0)

    def test_repeat_application_cannot_double_spend(self):
        src = self.credit_invoice()
        t = self.doc(price="1000")
        self.assertEqual(self.apply(src, [(t, "110")]).status_code, 200)
        r = self.apply(src, [(t, "110")])
        self.assertEqual(r.data["code"], "over_applied")
        self.assertEqual(self.cmodel.objects.get().amount, D("110"))

    def test_requires_actual_credit(self):
        plain = self.doc()
        t = self.doc()
        self.assertEqual(self.apply(plain, [(t, "1")]).data["code"], "over_applied")

    def test_wrong_party_and_foreign_org_rejected(self):
        src = self.credit_invoice()
        other_party = Party.objects.create(organization=self.bakery, name="Cust B", kind="customer" if self.side == "sales" else "supplier")
        t = self.doc(party=other_party)
        self.assertEqual(self.apply(src, [(t, "1")]).data["code"], "invalid_invoice")
        # another organization's invoice is invisible
        c_other = self.as_user(self.bob, self.other)
        self.assertEqual(c_other.post(f"{self.base}{src['id']}/apply-credit/", {"applications": []}, format="json").status_code, 404)
        self.assertEqual(self.apply(src, [({"id": 999999}, "1")]).status_code, 400)

    def test_void_and_return_documents_cannot_be_used(self):
        src = self.credit_invoice()
        void_inv = self.doc()
        self.client.post(f"{self.base}{void_inv['id']}/void/")
        self.assertEqual(self.apply(src, [(void_inv, "1")]).status_code, 400)
        r = self.client.get(f"{self.base}?kind=return").data
        ret = (r["results"] if isinstance(r, dict) else r)[0]
        self.assertEqual(self.apply(ret, [(self.doc(), "1")]).status_code, 400)

    def test_viewer_cannot_apply_but_can_read(self):
        br = Branch.objects.create(organization=self.bakery, name="V")
        src = self.credit_invoice(branch=br.id)
        t = self.doc(branch=br.id)
        vera = User.objects.create_user("vera", password="pw-12345-xx")
        m = Membership.objects.create(user=vera, organization=self.bakery, role=Membership.Role.VIEWER)
        BranchAccess.objects.create(membership=m, branch=br)
        cv = self.as_user(vera, self.bakery)
        self.assertEqual(self.apply(src, [(t, "1")], client=cv).status_code, 403)
        self.assertEqual(cv.get(f"{self.base}{src['id']}/credit/").status_code, 200)

    def test_unapply_restores_both_sides(self):
        src = self.credit_invoice()
        t = self.doc()
        self.apply(src, [(t, "50")])
        app_id = self.summary(src)["given"][0]["id"]
        r = self.client.post(f"{self.base}{src['id']}/unapply-credit/", {"application": app_id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(D(r.data["credit_available"]), D("110"))
        self.assertEqual(self.outstanding(t), D("110"))
        self.assertEqual(self.client.post(f"{self.base}{src['id']}/unapply-credit/", {"application": app_id}, format="json").status_code, 400)

    def test_credit_list_endpoint(self):
        src = self.credit_invoice()
        rows = self.client.get(f"{self.base}credits/?party={self.party.id}").data
        self.assertEqual([r["id"] for r in rows], [src["id"]])
        self.assertEqual(D(rows[0]["credit_available"]), D("110"))
        self.apply(src, [(self.doc(), "110")])
        self.assertEqual(self.client.get(f"{self.base}credits/").data, [])

    def test_period_lock_blocks_apply_and_unapply(self):
        src = self.credit_invoice()
        t = self.doc()
        self.assertEqual(self.apply(src, [(t, "10")], date="2026-02-01").status_code, 200)
        self.bakery.books_locked_through = date(2026, 2, 28)
        self.bakery.save()
        self.assertEqual(self.apply(src, [(t, "10")], date="2026-02-15").data["code"], "period_locked")
        app_id = self.summary(src)["given"][0]["id"]
        r = self.client.post(f"{self.base}{src['id']}/unapply-credit/", {"application": app_id}, format="json")
        self.assertEqual(r.data["code"], "period_locked")

    def test_date_rules(self):
        src = self.credit_invoice()                          # return dated 2026-01-20
        t = self.doc(on="2026-03-01")
        self.assertEqual(self.apply(src, [(t, "1")], date="2026-02-01").data["code"], "date_before_invoice")
        t2 = self.doc(on="2026-01-12")
        self.assertEqual(self.apply(src, [(t2, "1")], date="2026-01-19").data["code"], "date_before_credit")
        self.assertEqual(self.apply(src, [(t2, "1")], date="2999-01-01").data["code"], "future_date")

    # ---- voids
    def test_voiding_invoices_with_credit_links_is_blocked(self):
        src = self.credit_invoice()
        t = self.doc()
        self.apply(src, [(t, "10")])
        r = self.client.post(f"{self.base}{t['id']}/void/")
        self.assertEqual(r.data["code"], "has_credit_applications")
        self.assertEqual(self.client.post(f"{self.base}{src['id']}/void/").status_code, 400)

    def test_voiding_the_return_or_payment_that_funds_the_credit_is_blocked_while_applied(self):
        src = self.doc()
        pay = self.pay(src, "110")
        ret = self.ret(src, price="100")
        t = self.doc()
        self.apply(src, [(t, "110")])
        n = self.ledger_state()
        r = self.client.post(f"{self.base}{ret['id']}/void/")
        self.assertEqual(r.data["code"], "credit_applied")
        r = self.client.post(f"{self.cash_url}{pay['id']}/void/")
        self.assertEqual(r.data["code"], "credit_applied")
        self.assertEqual(self.ledger_state(), n)             # whole void rolled back, no half-written reversal
        # after unapplying, both voids work
        self.client.post(f"{self.base}{src['id']}/unapply-credit/", {"application": self.summary(src)["given"][0]["id"]}, format="json")
        self.assertEqual(self.client.post(f"{self.cash_url}{pay['id']}/void/").status_code, 200)

    def test_voiding_unrelated_receipt_allowed_and_partial_cover(self):
        src = self.credit_invoice(total_returned="110")
        t = self.doc()
        pay_t = self.pay(t, "110", on="2026-02-01")
        self.assertEqual(self.client.post(f"{self.cash_url}{pay_t['id']}/void/").status_code, 200)
        self.assertEqual(self.apply(src, [(t, "30")]).status_code, 200)
        # voiding a payment on the TARGET is fine (target has only received credit)
        self.assertEqual(self.outstanding(t), D("80"))

    # ---- aging / balances
    def test_aging_and_party_balance_reconcile_after_application(self):
        src = self.credit_invoice()
        t = self.doc(price="200")
        before = self.aging_total()
        self.apply(src, [(t, "100")])
        after = self.aging_total()
        self.assertEqual(self.outstanding(t), D("120"))
        # the application moves invoice-level balances only: totals still equal the (unchanged) ledger balance
        for snap in (before, after):
            self.assertEqual(D(str(snap["totals"]["outstanding"])), D("110"))
            self.assertEqual(D(str(snap["totals"]["unapplied"])), D("0"))
            self.assertEqual(D(str(snap["totals"]["ledger_balance"])), D("110"))
        self.assertEqual(self.aging_row_outstanding(after), D("110"))
        ctrl = self.balance("ACCOUNTS_RECEIVABLE" if self.side == "sales" else "ACCOUNTS_PAYABLE")
        self.assertEqual(abs(ctrl), D("110"))

    def test_cross_branch_application_rejected(self):
        n = Branch.objects.create(organization=self.bakery, name="N")
        s = Branch.objects.create(organization=self.bakery, name="S")
        src = self.credit_invoice(branch=n.id)
        t = self.doc(branch=s.id)
        self.assertEqual(self.apply(src, [(t, "1")]).data["code"], "branch_mismatch")
        self.assertEqual(self.apply(src, [(self.doc(branch=n.id), "1")]).status_code, 200)


class PurchaseCreditApplicationTests(CreditApplicationTests):
    side = "purchases"


class CreditBranchAccessTests(BranchBase, CreditBase):
    def setUp(self):
        super().setUp()

    def test_restricted_user_cannot_see_or_target_other_branch_invoices(self):
        src = self.credit_invoice(branch=self.north.id)
        t_n = self.doc(branch=self.north.id)
        t_s = self.doc(branch=self.south.id)
        # nora sees north only
        r = self.apply(src, [(t_s, "1")], client=self.c_nora)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("South", str(r.content))
        self.assertEqual(self.apply(src, [(t_n, "5")], client=self.c_nora).status_code, 200)
        # sam has no branches -> cannot see anything
        self.assertEqual(self.client.get(f"{self.base}{src['id']}/credit/").status_code, 200)
        self.assertEqual(self.c_sam.get(f"{self.base}{src['id']}/credit/").status_code, 404)
        self.assertEqual(self.c_sam.get(f"{self.base}credits/").data, [])
        self.assertEqual(self.apply(src, [(t_n, "1")], client=self.c_sam).status_code, 404)
        # viewer assigned to north can read but not write
        self.assertEqual(self.c_vic.get(f"{self.base}{src['id']}/credit/").status_code, 200)
        self.assertEqual(self.apply(src, [(t_n, "1")], client=self.c_vic).status_code, 403)

    def test_unbranched_credit_hidden_from_restricted_user(self):
        src = self.credit_invoice()
        self.assertEqual(self.c_nora.get(f"{self.base}{src['id']}/credit/").status_code, 404)


class CreditApplicationLedgerTests(CreditBase):
    def test_application_writes_no_journal_rows_and_reports_balance(self):
        src = self.credit_invoice()
        t = self.doc(price="500")
        n = self.ledger_state()
        self.apply(src, [(t, "60")])
        self.assertEqual(self.ledger_state(), n)
        self.assertTrue(self.trial_balance_ok())


class CreditConcurrencyTests(TransactionTestCase):
    """PostgreSQL only: SQLite serialises writers, so row-lock races cannot be exercised there."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization
        from rest_framework.test import APIClient

        self.user = User.objects.create_user("carl", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race Co")
        self.party = Party.objects.create(organization=self.org, name="C", kind="customer")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _post(self, url, body):
        r = self.client.post(url, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def _inv(self, price="100", **kw):
        return self._post(SALES, {"party": self.party.id, "date": "2026-01-10", **kw,
                                  "items": [{"description": "x", "quantity": "1", "unit_price": price, "tax_rate": "10"}]})

    def _credit(self):
        inv = self._inv()
        from apps.organizations.models import Organization  # noqa: F401
        from apps.ledger.models import Account

        bank = Account.objects.get(organization=self.org, system_code="BANK")
        pay = self._post("/api/v1/receipts/", {"party": self.party.id, "cash_account": bank.id, "amount": "110", "date": "2026-01-15",
                                               "allocations": [{"invoice": inv["id"], "amount": "110"}]})
        ret = self._inv(kind="return", original=inv["id"], date="2026-01-20")
        return inv, pay, ret

    def _race(self, *fns):
        barrier, out = threading.Barrier(len(fns)), []

        def run(fn):
            from django.db import connection as c
            try:
                barrier.wait()
                out.append(fn())
            finally:
                c.close()
        ts = [threading.Thread(target=run, args=(f,)) for f in fns]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out

    def _api(self):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(self.user)
        c.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return c

    def test_concurrent_applications_cannot_overspend_one_credit(self):
        src, _, _ = self._credit()
        targets = [self._inv(price="500") for _ in range(4)]

        def go(t):
            return lambda: self._api().post(f"{SALES}{src['id']}/apply-credit/", {"applications": [{"invoice": t["id"], "amount": "60"}]}, format="json").status_code
        codes = self._race(*[go(t) for t in targets])
        self.assertEqual(sorted(codes), [200, 400, 400, 400])      # 110 credit only covers one 60
        self.assertEqual(sum(a.amount for a in SalesCreditApplication.objects.all()), D("60"))

    def test_same_target_concurrent_applications(self):
        src, _, _ = self._credit()
        t = self._inv(price="30")                                   # 33 outstanding
        go = lambda: self._api().post(f"{SALES}{src['id']}/apply-credit/", {"applications": [{"invoice": t["id"], "amount": "33"}]}, format="json").status_code
        codes = self._race(go, go)
        self.assertEqual(sorted(codes), [200, 400])
        self.assertEqual(SalesCreditApplication.objects.get().amount, D("33"))

    def test_apply_racing_void_of_funding_receipt_stays_consistent(self):
        from apps.treasury.selectors import outstanding_invoices

        for _ in range(3):
            SalesCreditApplication.objects.all().delete()
            src, pay, _ = self._credit()
            t = self._inv(price="500")
            ap = lambda: self._api().post(f"{SALES}{src['id']}/apply-credit/", {"applications": [{"invoice": t["id"], "amount": "110"}]}, format="json").status_code
            vd = lambda: self._api().post(f"/api/v1/receipts/{pay['id']}/void/").status_code
            self._race(ap, vd)
            for r in outstanding_invoices(self.org, "receipt"):
                given = SalesCreditApplication.objects.filter(from_invoice=r).exists()
                if given:
                    self.assertLessEqual(r.outstanding, 0, "credit applied but source is no longer in surplus")
            from apps.ledger.models import JournalLine
            from django.db.models import Sum
            a = JournalLine.objects.filter(organization=self.org).aggregate(d=Sum("debit"), c=Sum("credit"))
            self.assertEqual(a["d"], a["c"])
