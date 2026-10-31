from django.contrib.auth import get_user_model

from apps.treasury.selectors import outstanding_invoices

from .base import AccountingTestCase, D


class AllocationTests(AccountingTestCase):
    def receipt(self, amount, allocs=None, **extra):
        body = {"party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": amount, "date": "2026-02-01"}
        if allocs is not None:
            body["allocations"] = [{"invoice": i, "amount": a} for i, a in allocs]
        return self.client.post("/api/v1/receipts/", {**body, **extra}, format="json")

    def outstanding(self, inv_id, direction="receipt"):
        return {i.id: i.outstanding for i in outstanding_invoices(self.bakery, direction)}[inv_id]

    def setUp(self):
        super().setUp()
        self.inv1 = self.sale(price="50", qty="2", tax="10").data["id"]  # 110
        self.inv2 = self.sale(price="100", qty="1", tax="0").data["id"]  # 100

    def test_partial_then_full_payment_of_one_invoice(self):
        self.assertEqual(self.receipt("40", [(self.inv1, "40")]).status_code, 201)
        self.assertEqual(self.outstanding(self.inv1), D("70.00"))
        self.assertEqual(self.receipt("70", [(self.inv1, "70")]).status_code, 201)
        self.assertEqual(self.outstanding(self.inv1), D("0.00"))
        self.assertEqual(self.outstanding(self.inv2), D("100.00"))

    def test_one_receipt_across_multiple_invoices_with_unapplied_remainder(self):
        r = self.receipt("250", [(self.inv1, "110"), (self.inv2, "100")])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((D(r.data["allocated"]), D(r.data["unapplied"])), (D("210.00"), D("40.00")))
        self.assertEqual(self.outstanding(self.inv1) + self.outstanding(self.inv2), D("0"))
        self.assertEqual(self.balance("BANK"), D("250.00"))  # cash fully received, unapplied stays on account

    def test_unapplied_can_be_allocated_later(self):
        rid = self.receipt("100").data["id"]
        r = self.client.post(f"/api/v1/receipts/{rid}/allocate/", {"allocations": [{"invoice": self.inv2, "amount": "60"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(r.data["unapplied"]), D("40.00"))
        over = self.client.post(f"/api/v1/receipts/{rid}/allocate/", {"allocations": [{"invoice": self.inv1, "amount": "50"}]}, format="json")
        self.assertEqual(over.data["code"], "over_allocated")

    def test_cannot_exceed_outstanding_or_amount(self):
        self.assertEqual(self.receipt("500", [(self.inv2, "100.01")]).data["code"], "over_applied")
        self.assertEqual(self.receipt("50", [(self.inv1, "60")]).data["code"], "over_allocated")

    def test_failed_allocation_rolls_back_the_receipt(self):
        self.receipt("500", [(self.inv2, "100.01")])
        self.assertEqual(self.client.get("/api/v1/receipts/").data["count"], 0)
        self.assertEqual(self.balance("BANK"), D("0"))

    def test_wrong_party_and_foreign_invoice_rejected(self):
        other = self.client.post("/api/v1/parties/", {"name": "Cust B", "kind": "customer"}, format="json").data["id"]
        r = self.receipt("10", [(self.inv1, "10")], party=other)
        self.assertEqual(r.data["code"], "invalid_invoice")
        bob = self.as_user(self.bob, self.other)
        from apps.parties.models import Party

        pb = Party.objects.create(organization=self.other, name="X", kind="customer")
        r = bob.post("/api/v1/receipts/", {"party": pb.id, "cash_account": self.acct("CASH", self.other).id, "amount": "5",
                     "date": "2026-02-01", "allocations": [{"invoice": self.inv1, "amount": "5"}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_voiding_receipt_restores_outstanding_and_blocks_invoice_void_while_applied(self):
        rid = self.receipt("110", [(self.inv1, "110")]).data["id"]
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{self.inv1}/void/").data["code"], "has_allocations")
        self.client.post(f"/api/v1/receipts/{rid}/void/")
        self.assertEqual(self.outstanding(self.inv1), D("110.00"))
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{self.inv1}/void/").status_code, 200)

    def test_returns_reduce_outstanding(self):
        self.client.post("/api/v1/sales/invoices/", {"kind": "return", "original": self.inv2, "party": self.customer.id, "date": "2026-02-02",
                         "items": [{"description": "x", "quantity": "1", "unit_price": "30"}]}, format="json")
        self.assertEqual(self.outstanding(self.inv2), D("70.00"))

    def test_open_invoices_endpoint_and_as_of(self):
        self.receipt("40", [(self.inv1, "40")])
        rows = self.client.get(f"/api/v1/receipts/open-invoices/?party={self.customer.id}").data
        self.assertEqual({r["id"]: D(r["outstanding"]) for r in rows}, {self.inv1: D("70.00"), self.inv2: D("100.00")})
        before = {i.id: i.outstanding for i in outstanding_invoices(self.bakery, "receipt", as_of="2026-01-31")}
        self.assertEqual(before[self.inv1], D("110.00"))  # receipt dated 2026-02-01 not yet counted

    def test_invoice_ledger_equals_sum_of_invoice_outstanding(self):
        self.receipt("40", [(self.inv1, "40")])
        total = sum(i.outstanding for i in outstanding_invoices(self.bakery, "receipt"))
        self.assertEqual(total, self.balance("ACCOUNTS_RECEIVABLE"))

    def test_payment_allocation_against_purchase_invoices(self):
        pi = self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-10", "items": [
            {"description": "Flour", "quantity": "1", "unit_price": "200"}]}, format="json").data["id"]
        r = self.client.post("/api/v1/payments/", {"party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": "150",
                             "date": "2026-02-01", "allocations": [{"invoice": pi, "amount": "120"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data["unapplied"]), D("30.00"))
        self.assertEqual(self.outstanding(pi, "payment"), D("80.00"))

    def test_due_date_defaults_and_validates(self):
        self.assertEqual(self.client.get(f"/api/v1/sales/invoices/{self.inv1}/").data["due_date"], "2026-01-10")
        r = self.sale(due_date="2026-01-01")
        self.assertEqual(r.data["code"], "invalid_due_date")


class VoucherPurposeTests(AccountingTestCase):
    def v(self, t, lines):
        return self.client.post("/api/v1/vouchers/", {"type": t, "date": "2026-02-01", "lines": lines}, format="json")

    def test_general_rejects_control_accounts_and_cash_only(self):
        ar = self.v("general", [{"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "5", "party": self.customer.id}, {"account": self.acct("EQUITY").id, "credit": "5"}])
        self.assertEqual(ar.data["code"], "general_control_account")
        cash = self.v("general", [{"account": self.acct("CASH").id, "debit": "5"}, {"account": self.acct("BANK").id, "credit": "5"}])
        self.assertEqual(cash.data["code"], "general_use_contra")

    def test_journal_may_adjust_receivables_but_needs_party(self):
        ok = self.v("journal", [{"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "5", "party": self.customer.id}, {"account": self.acct("SALES").id, "credit": "5"}])
        self.assertEqual(ok.status_code, 201, ok.data)
        bad = self.v("journal", [{"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "5"}, {"account": self.acct("SALES").id, "credit": "5"}])
        self.assertEqual(bad.data["code"], "party_required")

    def test_both_types_enforce_balance_and_have_separate_numbering(self):
        lines = [{"account": self.acct("CASH").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]
        self.assertEqual(self.v("general", lines).data["number"], "GV-000001")
        self.assertEqual(self.v("journal", lines).data["number"], "JV-000001")
        lines[1]["credit"] = "4"
        self.assertEqual(self.v("journal", lines).data["code"], "unbalanced")
        self.assertEqual(self.v("general", lines).data["code"], "unbalanced")
        self.assertEqual(self.client.get("/api/v1/vouchers/?type=general").data["count"], 1)


class AdminTests(AccountingTestCase):
    def test_admin_pages_load_and_posted_records_are_read_only(self):
        su = get_user_model().objects.create_superuser("root", "r@example.com", "pw-root-12345")
        self.client.force_login(su)
        self.sale()
        self.client.credentials()
        for url in ["organization", "branch", "membership", "party", "account"]:
            self.assertEqual(self.client.get(f"/admin/{'organizations' if url in ('organization','branch','membership') else 'parties' if url=='party' else 'ledger'}/{url}/").status_code, 200, url)
        for app, model in [("sales", "salesinvoice"), ("purchases", "purchaseinvoice"), ("treasury", "receipt"), ("treasury", "payment"), ("vouchers", "voucher"), ("ledger", "journaltransaction")]:
            self.assertEqual(self.client.get(f"/admin/{app}/{model}/").status_code, 200, model)
        self.assertEqual(self.client.get("/admin/sales/salesinvoice/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 200)

    def test_admin_void_action(self):
        from apps.sales.models import SalesInvoice

        su = get_user_model().objects.create_superuser("root", "r@example.com", "pw-root-12345")
        inv = self.sale().data["id"]
        c = self.client_class()
        c.force_login(su)
        c.post("/admin/sales/salesinvoice/", {"action": "void_selected", "_selected_action": [inv]})
        self.assertEqual(SalesInvoice.objects.get(pk=inv).status, "void")
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("0"))
