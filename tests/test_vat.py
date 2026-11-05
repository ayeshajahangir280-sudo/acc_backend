"""VAT on counter-account receipts/payments and the VAT return worksheet."""
from datetime import date

from django.contrib.auth import get_user_model

from apps.ledger.models import Account
from apps.ledger.services import Line, post_journal, system_account
from apps.organizations.models import Branch, BranchAccess, Membership

from .base import AccountingTestCase, D

User = get_user_model()
RET = "/api/v1/reports/vat-return/"


class VatBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.income = Account.objects.create(organization=self.bakery, code="4900", name="Other income", type="income")
        self.cost = Account.objects.create(organization=self.bakery, code="5900", name="Misc cost", type="expense")
        self.bank = self.acct("BANK")

    def balance_of(self, account):
        from django.db.models import Sum

        from apps.ledger.models import JournalLine

        a = JournalLine.objects.filter(account=account).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (a["d"] or D(0)) - (a["c"] or D(0))

    def receipt(self, amount, rate, on="2026-03-05", **kw):
        return self.client.post("/api/v1/receipts/", {"counter_account": self.income.id, "cash_account": self.bank.id, "amount": amount, "date": on, "tax_rate": rate, **kw}, format="json")

    def payment(self, amount, rate, on="2026-03-06", **kw):
        return self.client.post("/api/v1/payments/", {"counter_account": self.cost.id, "cash_account": self.bank.id, "amount": amount, "date": on, "tax_rate": rate, **kw}, format="json")

    def invoice(self, url, party, price, rate, kind="invoice", on="2026-03-01", **kw):
        r = self.client.post(url, {"party": party.id, "date": on, "kind": kind, "items": [{"description": "x", "quantity": "1", "unit_price": price, "tax_rate": rate}], **kw}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def report(self, client=None, **q):
        r = (client or self.client).get(RET, q)
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def row(self, section, rate):
        return next((r for r in section["rows"] if D(str(r["rate"])) == D(rate)), None)


class CashVatTests(VatBase):
    def test_receipt_with_vat_splits_income_and_output_tax(self):
        r = self.receipt("105", "5")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(r.data["tax_total"]), D("5.00"))
        self.assertEqual((self.balance("BANK"), self.balance("TAX_OUTPUT")), (D("105.00"), D("-5.00")))
        self.assertEqual(self.balance_of(self.income), D("-100.00"))
        self.assertTrue(self.trial_balance_ok())

    def test_payment_with_vat_posts_input_tax(self):
        r = self.payment("210", "5")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((self.balance("BANK"), self.balance("TAX_INPUT")), (D("-210.00"), D("10.00")))
        self.assertTrue(self.trial_balance_ok())

    def test_zero_rate_unchanged_and_validation(self):
        self.assertEqual(self.receipt("50", "0").status_code, 201)
        self.assertEqual(self.balance("TAX_OUTPUT"), D("0"))
        self.assertEqual(self.receipt("50", "150").data["code"], "invalid_tax_rate")
        r = self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.bank.id, "amount": "50", "date": "2026-03-05", "tax_rate": "5"}, format="json")
        self.assertEqual(r.data["code"], "tax_with_party")

    def test_void_reverses_tax(self):
        r = self.receipt("105", "5")
        self.assertEqual(self.client.post(f"/api/v1/receipts/{r.data['id']}/void/").status_code, 200)
        self.assertEqual(self.balance("TAX_OUTPUT"), D("0"))

    def test_period_lock_applies(self):
        self.bakery.books_locked_through = date(2026, 3, 31)
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(self.receipt("105", "5").data["code"], "period_locked")


class VatReturnTests(VatBase):
    def seed(self):
        self.invoice("/api/v1/sales/invoices/", self.customer, "1000", "10")            # out 100
        self.invoice("/api/v1/sales/invoices/", self.customer, "500", "0")              # zero-rated
        s = self.invoice("/api/v1/sales/invoices/", self.customer, "400", "10", on="2026-03-02")
        self.invoice("/api/v1/sales/invoices/", self.customer, "100", "10", kind="return", on="2026-03-03", original=s["id"])   # -10
        self.receipt("105", "5")                                                         # out 5
        self.invoice("/api/v1/purchases/invoices/", self.supplier, "300", "10")         # in 30
        self.client.post("/api/v1/expenses/", {"expense_account": self.cost.id, "paid_through": self.bank.id, "amount": "200", "tax_rate": "10", "date": "2026-03-04"}, format="json")  # in 20
        self.payment("210", "5")                                                         # in 10

    def test_totals_by_rate_and_reconciliation_to_ledger(self):
        self.seed()
        r = self.report()
        self.assertEqual(D(str(r["output"]["tax"])), D("135.00"))     # 100 + 40 - 10 (return) + 5 (receipt)
        self.assertEqual(D(str(r["input"]["tax"])), D("60.00"))
        self.assertEqual(D(str(r["net_payable"])), D("75.00"))
        self.assertEqual(D(str(self.row(r["output"], "0")["taxable"])), D("500.00"))
        self.assertEqual(D(str(self.row(r["output"], "10")["taxable"])), D("1300.00"))     # 1000 + 400 - 100
        self.assertEqual(D(str(r["other_ledger_adjustments"]["output_tax"])), D("0"))
        self.assertEqual(D(str(r["other_ledger_adjustments"]["input_tax"])), D("0"))

    def test_voided_documents_are_excluded_and_ledger_still_reconciles(self):
        a = self.invoice("/api/v1/sales/invoices/", self.customer, "1000", "10")
        self.client.post(f"/api/v1/sales/invoices/{a['id']}/void/")
        r = self.report()
        self.assertEqual(D(str(r["output"]["tax"])), D("0"))
        self.assertEqual(D(str(r["other_ledger_adjustments"]["output_tax"])), D("0"))

    def test_manual_journal_shows_as_adjustment(self):
        post_journal(self.bakery, date(2026, 3, 8), [Line(system_account(self.bakery, "TAX_OUTPUT"), credit=D("7")), Line(self.cost, debit=D("7"))], "manual", None, "J1", "", None, self.alice)
        r = self.report()
        self.assertEqual(D(str(r["other_ledger_adjustments"]["output_tax"])), D("7.00"))
        self.assertEqual(D(str(r["output"]["tax"])), D("0"))

    def test_date_window(self):
        self.seed()
        r = self.report(date_from="2026-03-04", date_to="2026-03-31")
        self.assertEqual(D(str(r["output"]["tax"])), D("5.00"))        # the return (3 Mar) is outside the window; the receipt (5 Mar) is inside

    def test_isolation_and_branch_scope(self):
        self.seed()
        self.assertEqual(D(str(self.report(client=self.as_user(self.bob, self.other))["output"]["tax"])), D("0"))
        n = Branch.objects.create(organization=self.bakery, name="N")
        nora = User.objects.create_user("nora", password="pw-12345-xx")
        m = Membership.objects.create(user=nora, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        BranchAccess.objects.create(membership=m, branch=n)
        cn = self.as_user(nora, self.bakery)
        self.assertEqual(D(str(self.report(client=cn)["output"]["tax"])), D("0"))     # all seeded documents are unbranched
        r = self.client.post("/api/v1/sales/invoices/", {"party": self.customer.id, "date": "2026-03-09", "branch": n.id, "items": [{"description": "x", "quantity": "1", "unit_price": "100", "tax_rate": "10"}]}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(D(str(self.report(client=cn)["output"]["tax"])), D("10.00"))
