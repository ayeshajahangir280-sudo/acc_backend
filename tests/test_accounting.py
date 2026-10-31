from decimal import Decimal

from django.db import IntegrityError, transaction

from apps.core.errors import DomainError
from apps.ledger.models import JournalLine, JournalTransaction
from apps.ledger.services import Line, post_journal
from apps.sales.models import SalesInvoice

from .base import AccountingTestCase, D


class LedgerEngineTests(AccountingTestCase):
    def test_unbalanced_entry_rejected_and_nothing_saved(self):
        cash, eq = self.acct("CASH"), self.acct("EQUITY")
        with self.assertRaises(DomainError):
            post_journal(self.bakery, "2026-01-01", [Line(cash, debit=D(100)), Line(eq, credit=D(99))], "t")
        self.assertEqual(JournalTransaction.objects.count(), 0)
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_line_must_be_one_sided(self):
        cash, eq = self.acct("CASH"), self.acct("EQUITY")
        with self.assertRaises(DomainError):
            post_journal(self.bakery, "2026-01-01", [Line(cash, debit=D(5), credit=D(5)), Line(eq, credit=D(0))], "t")

    def test_db_constraint_blocks_two_sided_line(self):
        txn = post_journal(self.bakery, "2026-01-01", [Line(self.acct("CASH"), debit=D(5)), Line(self.acct("EQUITY"), credit=D(5))], "t")
        with self.assertRaises(IntegrityError), transaction.atomic():
            JournalLine.objects.create(organization=self.bakery, transaction=txn, date="2026-01-01",
                                       account=self.acct("CASH"), debit=D(1), credit=D(1))

    def test_cross_tenant_account_rejected(self):
        foreign = self.acct("CASH", self.other)
        with self.assertRaises(DomainError):
            post_journal(self.bakery, "2026-01-01", [Line(foreign, debit=D(5)), Line(self.acct("EQUITY"), credit=D(5))], "t")


class SalesTests(AccountingTestCase):
    def test_sale_posts_balanced_entries_and_updates_customer_balance(self):
        r = self.sale()  # 2 x 50 = 100 net, 10% tax = 10
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data["subtotal"]), D("100.00"))
        self.assertEqual(D(r.data["tax_total"]), D("10.00"))
        self.assertEqual(D(r.data["total"]), D("110.00"))
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("110.00"))
        self.assertEqual(self.balance("SALES"), D("-100.00"))
        self.assertEqual(self.balance("TAX_OUTPUT"), D("-10.00"))
        self.assertTrue(self.trial_balance_ok())
        bal = self.client.get("/api/v1/party-balances/").data
        self.assertEqual(D(bal[0]["receivable"]), D("110.00"))

    def test_idempotency_key_prevents_double_posting(self):
        a = self.sale(idempotency_key="abc")
        b = self.sale(idempotency_key="abc")
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(a.data["id"], b.data["id"])
        self.assertEqual(SalesInvoice.objects.count(), 1)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("110.00"))

    def test_invalid_sale_leaves_nothing_behind(self):
        r = self.sale(qty="0")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SalesInvoice.objects.count(), 0)
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_supplier_cannot_be_invoiced_as_customer(self):
        r = self.client.post("/api/v1/sales/invoices/", {"party": self.supplier.id, "date": "2026-01-10", "items": [
            {"description": "x", "quantity": "1", "unit_price": "5"}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_return_reduces_balance_and_cannot_exceed_original(self):
        inv = self.sale().data
        ret = {"kind": "return", "original": inv["id"], "party": self.customer.id, "date": "2026-01-12",
               "items": [{"description": "Bread", "quantity": "1", "unit_price": "50", "tax_rate": "10"}]}
        r = self.client.post("/api/v1/sales/invoices/", ret, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("55.00"))
        self.assertEqual(self.balance("SALES_RETURNS"), D("50.00"))
        # another 1.5 units would exceed what is left (55 returnable)
        ret["items"][0]["quantity"] = "1.5"
        r2 = self.client.post("/api/v1/sales/invoices/", ret, format="json")
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.data["code"], "over_return")
        self.assertTrue(self.trial_balance_ok())

    def test_void_reverses_and_cannot_repeat(self):
        inv = self.sale().data
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{inv['id']}/void/").status_code, 200)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("0"))
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{inv['id']}/void/").status_code, 400)
        self.assertTrue(self.trial_balance_ok())

    def test_cannot_void_invoice_with_active_return(self):
        inv = self.sale().data
        self.client.post("/api/v1/sales/invoices/", {"kind": "return", "original": inv["id"], "party": self.customer.id,
                         "date": "2026-01-12", "items": [{"description": "B", "quantity": "1", "unit_price": "10"}]}, format="json")
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{inv['id']}/void/").data["code"], "has_returns")

    def test_posted_documents_cannot_be_edited_or_deleted(self):
        inv = self.sale().data
        self.assertEqual(self.client.delete(f"/api/v1/sales/invoices/{inv['id']}/").status_code, 405)
        self.assertEqual(self.client.patch(f"/api/v1/sales/invoices/{inv['id']}/", {"total": "1"}).status_code, 405)

    def test_numbers_are_sequential(self):
        self.assertEqual(self.sale().data["number"], "SI-000001")
        self.assertEqual(self.sale().data["number"], "SI-000002")


class PurchaseTests(AccountingTestCase):
    def purchase(self, **extra):
        return self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-10", "items": [
            {"description": "Flour", "quantity": "10", "unit_price": "20", "tax_rate": "5"}], **extra}, format="json")

    def test_purchase_posts_and_updates_supplier_balance(self):
        r = self.purchase()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data["total"]), D("210.00"))
        self.assertEqual(self.balance("PURCHASES"), D("200.00"))
        self.assertEqual(self.balance("TAX_INPUT"), D("10.00"))
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("-210.00"))
        self.assertEqual(D(self.client.get("/api/v1/party-balances/").data[0]["payable"]), D("210.00"))
        self.assertTrue(self.trial_balance_ok())

    def test_purchase_return(self):
        inv = self.purchase().data
        r = self.client.post("/api/v1/purchases/invoices/", {"kind": "return", "original": inv["id"], "party": self.supplier.id,
                             "date": "2026-01-11", "items": [{"description": "Flour", "quantity": "10", "unit_price": "20", "tax_rate": "5"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("0"))
        self.assertEqual(self.balance("PURCHASE_RETURNS"), D("-200.00"))
        self.assertTrue(self.trial_balance_ok())


class TreasuryTests(AccountingTestCase):
    def test_receipt_settles_customer_balance(self):
        self.sale()
        r = self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("BANK").id,
                             "amount": "110.00", "date": "2026-01-15", "method": "bank_transfer", "reference": "TX1"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("0"))
        self.assertEqual(self.balance("BANK"), D("110.00"))

    def test_payment_settles_supplier_balance(self):
        self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-10", "items": [
            {"description": "F", "quantity": "1", "unit_price": "100"}]}, format="json")
        r = self.client.post("/api/v1/payments/", {"party": self.supplier.id, "cash_account": self.acct("CASH").id,
                             "amount": "100", "date": "2026-01-16"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("0"))
        self.assertEqual(self.balance("CASH"), D("-100.00"))

    def test_cash_account_must_be_cash_or_bank(self):
        r = self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("SALES").id,
                             "amount": "5", "date": "2026-01-15"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_party_and_counter_account_are_exclusive(self):
        r = self.client.post("/api/v1/payments/", {"cash_account": self.acct("CASH").id, "amount": "5", "date": "2026-01-15"}, format="json")
        self.assertEqual(r.data["code"], "party_or_account")

    def test_receipt_idempotency(self):
        body = {"party": self.customer.id, "cash_account": self.acct("CASH").id, "amount": "5", "date": "2026-01-15", "idempotency_key": "k1"}
        self.client.post("/api/v1/receipts/", body, format="json")
        self.client.post("/api/v1/receipts/", body, format="json")
        self.assertEqual(self.balance("CASH"), D("5"))


class VoucherTests(AccountingTestCase):
    def voucher(self, vtype, lines):
        return self.client.post("/api/v1/vouchers/", {"type": vtype, "date": "2026-02-01", "lines": lines}, format="json")

    def test_journal_voucher_multi_line_balanced(self):
        r = self.voucher("journal", [
            {"account": self.acct("CASH").id, "debit": "70"}, {"account": self.acct("BANK").id, "debit": "30"},
            {"account": self.acct("EQUITY").id, "credit": "100"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(r.data["total"]), D("100.00"))
        self.assertEqual(self.client.get("/api/v1/vouchers/?type=journal").data["count"], 1)

    def test_unbalanced_voucher_rejected(self):
        r = self.voucher("general", [{"account": self.acct("CASH").id, "debit": "70"}, {"account": self.acct("EQUITY").id, "credit": "60"}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "unbalanced")
        self.assertEqual(self.client.get("/api/v1/vouchers/").data["count"], 0)
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_contra_only_cash_bank(self):
        ok = self.voucher("contra", [{"account": self.acct("CASH").id, "credit": "50"}, {"account": self.acct("BANK").id, "debit": "50"}])
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(self.balance("BANK"), D("50"))
        bad = self.voucher("contra", [{"account": self.acct("CASH").id, "credit": "50"}, {"account": self.acct("SALES").id, "debit": "50"}])
        self.assertEqual(bad.data["code"], "contra_accounts")

    def test_voucher_void(self):
        v = self.voucher("general", [{"account": self.acct("CASH").id, "debit": "9"}, {"account": self.acct("EQUITY").id, "credit": "9"}]).data
        self.client.post(f"/api/v1/vouchers/{v['id']}/void/")
        self.assertEqual(self.balance("CASH"), D("0"))


class IsolationAndPermissionTests(AccountingTestCase):
    def test_org_data_is_isolated(self):
        self.sale()
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get("/api/v1/sales/invoices/").data["count"], 0)
        self.assertEqual(bob.get("/api/v1/parties/").data["count"], 0)
        self.assertEqual(bob.get("/api/v1/journal/").data["count"], 0)

    def test_user_cannot_select_foreign_org(self):
        c = self.as_user(self.bob, self.bakery)
        self.assertEqual(c.get("/api/v1/sales/invoices/").status_code, 403)

    def test_missing_org_header_rejected(self):
        c = self.as_user(self.alice, self.bakery)
        c.credentials()
        self.assertEqual(c.get("/api/v1/sales/invoices/").status_code, 403)

    def test_cannot_reference_foreign_objects(self):
        # Bob posts a sale to Alice's customer id, and Alice uses Bob's account in a voucher
        bob = self.as_user(self.bob, self.other)
        r = bob.post("/api/v1/sales/invoices/", {"party": self.customer.id, "date": "2026-01-01", "items": [
            {"description": "x", "quantity": "1", "unit_price": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/vouchers/", {"type": "general", "date": "2026-01-01", "lines": [
            {"account": self.acct("CASH", self.other).id, "debit": "1"}, {"account": self.acct("EQUITY").id, "credit": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_foreign_document_not_retrievable(self):
        inv = self.sale().data
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(f"/api/v1/sales/invoices/{inv['id']}/").status_code, 404)
        self.assertEqual(bob.post(f"/api/v1/sales/invoices/{inv['id']}/void/").status_code, 404)

    def test_viewer_is_read_only(self):
        from apps.organizations.models import Membership

        Membership.objects.create(user=self.bob, organization=self.bakery, role="viewer")
        c = self.as_user(self.bob, self.bakery)
        self.assertEqual(c.get("/api/v1/sales/invoices/").status_code, 200)
        self.assertEqual(c.post("/api/v1/parties/", {"name": "x", "kind": "customer"}).status_code, 403)

    def test_organization_list_only_shows_memberships(self):
        names = [o["name"] for o in self.client.get("/api/v1/organizations/").data["results"]]
        self.assertEqual(names, ["Bakery"])

    def test_login_and_token_auth(self):
        from rest_framework.test import APIClient

        c = APIClient()
        self.assertEqual(c.post("/api/v1/auth/login/", {"username": "alice", "password": "bad"}).status_code, 400)
        tok = c.post("/api/v1/auth/login/", {"username": "alice", "password": "pw-alice-123"}).data["token"]
        c.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        self.assertEqual(c.get("/api/v1/organizations/").status_code, 200)
        self.assertEqual(APIClient().get("/api/v1/organizations/").status_code, 401)

    def test_registers_filter_and_paginate(self):
        for _ in range(3):
            self.sale()
        r = self.client.get("/api/v1/sales/invoices/?date__gte=2026-01-01&date__lte=2026-01-31&page_size=2")
        self.assertEqual((r.data["count"], len(r.data["results"])), (3, 2))
        self.assertEqual(self.client.get("/api/v1/sales/invoices/?search=Cust").data["count"], 3)

    def test_register_query_count_is_constant(self):
        for _ in range(5):
            self.sale()
        with self.assertNumQueries(3):  # membership, count, page: constant, no per-row queries
            self.client.get("/api/v1/sales/invoices/")
