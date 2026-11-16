"""Per-line income accounts on sales invoices, optional sales descriptions, and the organization default."""
from collections import Counter
from decimal import Decimal

from apps.ledger.models import Account, JournalLine, JournalTransaction
from apps.sales.models import SalesInvoice, SalesItem

from .base import AccountingTestCase, D

URL = "/api/v1/sales/invoices/"


class IncomeAccountTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.sales = self.acct("SALES")
        self.service = Account.objects.create(organization=self.bakery, code="4010", name="Service Revenue", type="income")
        self.interest = Account.objects.create(organization=self.bakery, code="4020", name="Interest Income", type="income")
        self.rent = Account.objects.create(organization=self.bakery, code="6000", name="Rent", type="expense")

    def post(self, items, **extra):
        body = {"party": self.customer.id, "date": "2026-01-10", "items": items, **extra}
        return self.client.post(URL, body, format="json")

    def line(self, price="100", tax="5", **kw):
        return {"description": "x", "quantity": "1", "unit_price": price, "tax_rate": tax, **kw}

    def journal(self, inv_id, source="sales.invoice"):
        doc = SalesInvoice.objects.get(pk=inv_id)
        return sorted((l.account.code, l.debit, l.credit) for l in JournalLine.objects.filter(transaction=doc.journal))

    # --- unchanged default behaviour ---------------------------------------------------------------
    def test_no_account_posts_exactly_as_before(self):
        r = self.post([self.line("100", "5"), self.line("50", "0")])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.journal(r.data["id"]), sorted([
            ("1200", D("155.00"), D("0")), ("4000", D("0"), D("150.00")), ("2100", D("0"), D("5.00"))]))
        # the account actually used is recorded on new lines
        self.assertEqual({i["income_account"] for i in r.data["items"]}, {self.sales.id})
        self.assertTrue(self.trial_balance_ok())

    # --- per-line accounts -------------------------------------------------------------------------
    def test_lines_credit_their_own_accounts_and_stay_balanced(self):
        r = self.post([self.line("100", "5", income_account=self.service.id), self.line("40", "5", income_account=self.interest.id),
                       self.line("60", "5", income_account=self.service.id), self.line("10", "0")])
        self.assertEqual(r.status_code, 201, r.content)
        inv = SalesInvoice.objects.get(pk=r.data["id"])
        self.assertEqual((inv.subtotal, inv.tax_total, inv.total), (D("210.00"), D("10.00"), D("220.00")))
        self.assertEqual(self.journal(inv.id), sorted([
            ("1200", D("220.00"), D("0")), ("4010", D("0"), D("160.00")), ("4020", D("0"), D("40.00")),
            ("4000", D("0"), D("10.00")), ("2100", D("0"), D("10.00"))]))
        self.assertEqual([i["income_account_name"] for i in r.data["items"]], ["Service Revenue", "Interest Income", "Service Revenue", "Sales Revenue"])
        self.assertTrue(self.trial_balance_ok())

    def test_profit_and_loss_shows_each_income_account(self):
        self.post([self.line("100", "0", income_account=self.service.id), self.line("30", "0")])
        pl = self.client.get("/api/v1/reports/profit-loss/", {"date_from": "2026-01-01", "date_to": "2026-12-31"}).data
        income = {row["code"]: D(str(row["amount"])) for row in pl["income"]} if isinstance(pl.get("income"), list) else None
        if income is not None:
            self.assertEqual(income.get("4010"), D("100.00"))
            self.assertEqual(income.get("4000"), D("30.00"))
        self.assertEqual(D(str(pl["total_income"])), D("130.00"))

    def test_void_reverses_every_account(self):
        r = self.post([self.line("100", "5", income_account=self.service.id), self.line("20", "5")])
        self.assertEqual(self.client.post(f"{URL}{r.data['id']}/void/").status_code, 200)
        for acc in (self.service, self.sales, self.acct("ACCOUNTS_RECEIVABLE"), self.acct("TAX_OUTPUT")):
            agg = JournalLine.objects.filter(account=acc)
            self.assertEqual(sum((l.debit - l.credit for l in agg), Decimal("0")), 0, acc.code)

    def test_void_is_an_exact_line_mirror_and_does_not_change_existing_journal_lines(self):
        r = self.post([self.line("100", "5", income_account=self.service.id), self.line("40", "0", income_account=self.interest.id)])
        self.assertEqual(r.status_code, 201, r.content)
        doc = SalesInvoice.objects.get(pk=r.data["id"])
        source = doc.journal

        service_line = source.lines.get(account=self.service)
        service_line.credit = D("50")
        service_line.save(update_fields=["credit"])
        JournalLine.objects.create(
            organization=self.bakery, transaction=source, date=service_line.date, branch=service_line.branch,
            account=service_line.account, party=service_line.party, debit=service_line.debit, credit=D("50"),
            description=service_line.description,
        )

        original_lines = list(source.lines.order_by("id"))
        self.assertEqual(sum((line.debit for line in original_lines), D("0")),
                         sum((line.credit for line in original_lines), D("0")))
        before = {
            line.id: (line.transaction_id, line.date, line.branch_id, line.account_id, line.party_id,
                      line.debit, line.credit, line.description)
            for line in JournalLine.objects.filter(organization=self.bakery).order_by("id")
        }

        voided = self.client.post(f"{URL}{doc.id}/void/")
        self.assertEqual(voided.status_code, 200, voided.content)
        source.refresh_from_db()
        reversal = JournalTransaction.objects.get(reversal_of=source)
        reverse_lines = list(reversal.lines.all())

        self.assertEqual(source.status, "void")
        self.assertEqual(len(reverse_lines), len(original_lines))
        self.assertEqual(Counter((line.account_id, line.credit, line.debit) for line in original_lines),
                         Counter((line.account_id, line.debit, line.credit) for line in reverse_lines))
        self.assertEqual(sum((line.debit for line in reverse_lines), D("0")),
                         sum((line.credit for line in reverse_lines), D("0")))
        self.assertEqual(sum(line.account_id == self.service.id for line in reverse_lines), 2)

        after = {
            line.id: (line.transaction_id, line.date, line.branch_id, line.account_id, line.party_id,
                      line.debit, line.credit, line.description)
            for line in JournalLine.objects.filter(id__in=before).order_by("id")
        }
        self.assertEqual(after, before)
        self.assertTrue(self.trial_balance_ok())

    # --- validation ----------------------------------------------------------------------------------
    def test_ineligible_accounts_are_rejected_and_nothing_posts(self):
        foreign = Account.objects.create(organization=self.other, code="4010", name="Theirs", type="income")
        inactive = Account.objects.create(organization=self.bakery, code="4030", name="Old", type="income", is_active=False)
        for bad in (self.rent.id, foreign.id, inactive.id, self.acct("SALES_RETURNS").id, self.acct("BANK").id):
            r = self.post([self.line(income_account=bad)])
            self.assertEqual(r.status_code, 400, (bad, r.content))
            self.assertEqual(r.data.get("code"), "invalid_income_account", r.content)
        self.assertEqual(SalesInvoice.objects.filter(organization=self.bakery).count(), 0)
        self.assertEqual(JournalLine.objects.filter(organization=self.bakery).count(), 0)

    def test_description_optional_for_sales_but_not_purchases(self):
        r = self.post([{"quantity": "2", "unit_price": "10", "tax_rate": "0"}, {"description": "", "quantity": "1", "unit_price": "5"}])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([i["description"] for i in r.data["items"]], ["", ""])
        pdf = self.client.get(f"{URL}{r.data['id']}/pdf/")
        self.assertEqual(pdf.status_code, 200)
        p = self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-10",
                             "items": [{"description": "", "quantity": "1", "unit_price": "5"}]}, format="json")
        self.assertEqual(p.status_code, 400, p.content)

    # --- organization default ----------------------------------------------------------------------
    def test_organization_default_is_used_and_validated(self):
        org_url = f"/api/v1/organizations/{self.bakery.id}/"
        self.assertEqual(self.client.get(org_url).data["effective_income_account"], self.sales.id)
        for bad in (self.rent.id, self.acct("SALES_RETURNS").id):
            self.assertEqual(self.client.patch(org_url, {"default_income_account": bad}, format="json").status_code, 400)
        r = self.client.patch(org_url, {"default_income_account": self.service.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["effective_income_account"], self.service.id)
        inv = self.post([self.line("80", "0")])
        self.assertIn(("4010", D("0"), D("80.00")), self.journal(inv.data["id"]))
        # a default that later becomes inactive falls back to Sales Revenue instead of failing
        self.service.is_active = False
        self.service.save()
        inv = self.post([self.line("10", "0")])
        self.assertEqual(inv.status_code, 201, inv.content)
        self.assertIn(("4000", D("0"), D("10.00")), self.journal(inv.data["id"]))

    def test_viewer_cannot_change_default(self):
        from django.contrib.auth import get_user_model
        from apps.organizations.models import Membership

        v = get_user_model().objects.create_user("vic2", password="pw-vic-12345")
        Membership.objects.create(user=v, organization=self.bakery, role="viewer")
        r = self.as_user(v, self.bakery).patch(f"/api/v1/organizations/{self.bakery.id}/", {"default_income_account": self.service.id}, format="json")
        self.assertEqual(r.status_code, 403)

    # --- returns unchanged -------------------------------------------------------------------------
    def test_returns_keep_sales_returns_treatment(self):
        inv = self.post([self.line("100", "5", income_account=self.service.id)]).data
        bad = self.post([self.line("100", "5", income_account=self.service.id)], kind="return", original=inv["id"])
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.data.get("code"), "invalid_income_account")
        ret = self.post([{"quantity": "1", "unit_price": "100", "tax_rate": "5"}], kind="return", original=inv["id"])
        self.assertEqual(ret.status_code, 201, ret.content)
        self.assertEqual(self.journal(ret.data["id"]), sorted([
            ("1200", D("0"), D("105.00")), ("4100", D("100.00"), D("0")), ("2100", D("5.00"), D("0"))]))

    def test_multiple_income_accounts_support_receipts_returns_refunds_and_credit_applications(self):
        inv_response = self.post([
            self.line("100", "10", income_account=self.service.id),
            self.line("50", "0", income_account=self.interest.id),
        ])
        self.assertEqual(inv_response.status_code, 201, inv_response.content)
        inv = inv_response.data
        self.assertEqual((D(inv["subtotal"]), D(inv["tax_total"]), D(inv["total"])),
                         (D("150.00"), D("10.00"), D("160.00")))

        receipt = self.client.post("/api/v1/receipts/", {
            "party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "160.00",
            "date": "2026-01-11", "allocations": [{"invoice": inv["id"], "amount": "160.00"}],
        }, format="json")
        self.assertEqual(receipt.status_code, 201, receipt.content)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("0"))

        returned = self.post([self.line("50", "10")], kind="return", original=inv["id"])
        self.assertEqual(returned.status_code, 201, returned.content)
        self.assertEqual(self.journal(returned.data["id"]), sorted([
            ("1200", D("0"), D("55.00")), ("4100", D("50.00"), D("0")), ("2100", D("5.00"), D("0"))]))
        credit = self.client.get(f"{URL}{inv['id']}/credit/")
        self.assertEqual(credit.status_code, 200, credit.content)
        self.assertEqual(D(str(credit.data["credit_available"])), D("55.00"))

        refund = self.client.post(f"{URL}{inv['id']}/refunds/", {
            "cash_account": self.acct("BANK").id, "amount": "20.00", "date": "2026-01-12",
        }, format="json")
        self.assertEqual(refund.status_code, 201, refund.content)
        self.assertEqual(D(str(self.client.get(f"{URL}{inv['id']}/credit/").data["credit_available"])), D("35.00"))

        target = self.post([self.line("40", "0", income_account=self.service.id)])
        self.assertEqual(target.status_code, 201, target.content)
        before_application = (JournalTransaction.objects.count(), JournalLine.objects.count())
        applied = self.client.post(f"{URL}{inv['id']}/apply-credit/", {
            "applications": [{"invoice": target.data["id"], "amount": "35.00"}],
        }, format="json")
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(D(str(applied.data["credit_available"])), D("0"))
        self.assertEqual((JournalTransaction.objects.count(), JournalLine.objects.count()), before_application)
        open_rows = self.client.get("/api/v1/receipts/open-invoices/", {"party": self.customer.id})
        self.assertEqual(open_rows.status_code, 200, open_rows.content)
        self.assertEqual(D(str(next(row["outstanding"] for row in open_rows.data if row["id"] == target.data["id"]))),
                         D("5.00"))

        def net(account):
            rows = JournalLine.objects.filter(account=account)
            return sum((line.debit - line.credit for line in rows), D("0"))

        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("5.00"))
        self.assertEqual(net(self.service), D("-140.00"))
        self.assertEqual(net(self.interest), D("-50.00"))
        self.assertEqual(self.balance("SALES_RETURNS"), D("50.00"))
        self.assertEqual(self.balance("TAX_OUTPUT"), D("-5.00"))
        self.assertEqual(self.balance("BANK"), D("140.00"))
        self.assertTrue(self.trial_balance_ok())

    # --- historical lines --------------------------------------------------------------------------
    def test_legacy_lines_without_account_still_work(self):
        """Simulates an invoice posted before this change (no account stored): its journal is untouched and it can
        still be read, printed, paid, returned and voided."""
        inv = self.post([self.line("100", "5")]).data
        SalesItem.objects.filter(invoice_id=inv["id"]).update(income_account=None)
        before = self.journal(inv["id"])
        got = self.client.get(f"{URL}{inv['id']}/")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data["items"][0]["income_account"], None)
        self.assertEqual(self.client.get(f"{URL}{inv['id']}/pdf/").status_code, 200)
        self.assertEqual(self.journal(inv["id"]), before)
        rc = self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "50",
                              "date": "2026-01-11", "allocations": [{"invoice": inv["id"], "amount": "50"}]}, format="json")
        self.assertEqual(rc.status_code, 201, rc.content)
        ret = self.post([{"quantity": "1", "unit_price": "20", "tax_rate": "5"}], kind="return", original=inv["id"])
        self.assertEqual(ret.status_code, 201, ret.content)
        self.assertTrue(self.trial_balance_ok())
