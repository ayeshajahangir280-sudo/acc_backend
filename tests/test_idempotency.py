from unittest import mock

from django.db import IntegrityError

from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.ledger.models import JournalLine, JournalTransaction

from .test_inventory_dispatch import StockBase, API
from .base import D


def flaky_lookup():
    """First lookup misses, as if the competing request had not committed yet; later lookups are real."""
    calls = {"n": 0}

    def fn(model, org, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else find_existing(model, org, key)

    return fn, calls


class DecoratorTests(StockBase):
    def test_retries_once_when_a_key_is_supplied(self):
        seen = []

        @retry_on_key_conflict
        def op(*, idempotency_key=None):
            seen.append(1)
            if len(seen) == 1:
                raise IntegrityError("unique")
            return "ok"

        self.assertEqual(op(idempotency_key="k"), "ok")
        self.assertEqual(len(seen), 2)

    def test_does_not_retry_or_hide_errors_without_a_key_or_on_a_second_failure(self):
        calls = []

        @retry_on_key_conflict
        def op(*, idempotency_key=None):
            calls.append(1)
            raise IntegrityError("other constraint")

        with self.assertRaises(IntegrityError):
            op()
        self.assertEqual(len(calls), 1)
        calls.clear()
        with self.assertRaises(IntegrityError):
            op(idempotency_key="k")
        self.assertEqual(len(calls), 2)  # one retry, then the genuine error surfaces


class RaceTests(StockBase):
    """Each module: a normal create, then the same key arrives while the lookup 'misses' (simulated race)."""

    def check(self, module, url, body, count_model):
        first = self.client.post(url, {**body, "idempotency_key": "race-1"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        lines_before, txns_before = JournalLine.objects.count(), JournalTransaction.objects.count()
        lookup, calls = flaky_lookup()
        with mock.patch(f"{module}.find_existing", side_effect=lookup):
            second = self.client.post(url, {**body, "idempotency_key": "race-1"}, format="json")
        self.assertEqual(second.status_code, 200, second.data)        # original returned, not a 500
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(calls["n"], 2)                               # missed once, found on the retry
        self.assertEqual(count_model.objects.count(), 1)              # no second copy
        self.assertEqual((JournalLine.objects.count(), JournalTransaction.objects.count()), (lines_before, txns_before))
        return first

    def test_sales_invoice_and_numbering_has_no_gap(self):
        from apps.sales.models import SalesInvoice

        body = {"party": self.customer.id, "date": "2026-01-10", "items": [{"description": "x", "quantity": "1", "unit_price": "10", "tax_rate": "5"}]}
        first = self.check("apps.sales.services", f"{API}/sales/invoices/", body, SalesInvoice)
        nxt = self.client.post(f"{API}/sales/invoices/", {**body, "idempotency_key": "other"}, format="json")
        self.assertEqual((first.data["number"], nxt.data["number"]), ("SI-000001", "SI-000002"))
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("21.00"))  # 10.50 posted exactly twice, never three times

    def test_purchase_invoice(self):
        from apps.purchases.models import PurchaseInvoice

        body = {"party": self.supplier.id, "date": "2026-01-10", "items": [{"description": "x", "quantity": "1", "unit_price": "10"}]}
        self.check("apps.purchases.services", f"{API}/purchases/invoices/", body, PurchaseInvoice)

    def test_receipt_and_allocation_are_not_doubled(self):
        from apps.treasury.models import Receipt, ReceiptAllocation

        inv = self.sale(qty="1", price="100", tax="0").data["id"]
        body = {"party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "60", "date": "2026-01-11",
                "allocations": [{"invoice": inv, "amount": "60"}]}
        self.check("apps.treasury.services", f"{API}/receipts/", body, Receipt)
        self.assertEqual(ReceiptAllocation.objects.count(), 1)
        self.assertEqual(ReceiptAllocation.objects.get().amount, D("60.00"))
        self.assertEqual(self.client.get(f"{API}/receipts/open-invoices/?party={self.customer.id}").data[0]["outstanding"], "40.00")

    def test_payment(self):
        from apps.treasury.models import Payment

        body = {"party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": "5", "date": "2026-01-11"}
        self.check("apps.treasury.services", f"{API}/payments/", body, Payment)

    def test_voucher(self):
        from apps.vouchers.models import Voucher

        body = {"type": "journal", "date": "2026-01-11", "lines": [{"account": self.acct("CASH").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]}
        self.check("apps.vouchers.services", f"{API}/vouchers/", body, Voucher)
        self.assertEqual(self.balance("CASH"), D("5"))

    def test_production_does_not_consume_stock_twice(self):
        from apps.inventory.models import ProductionOrder

        self.adjust(self.flour, "10")
        body = {"date": "2026-03-02", "item": self.cake, "quantity": "1", "inputs": [{"item": self.flour, "quantity": "4"}]}
        self.check("apps.inventory.services", f"{API}/inventory/production/", body, ProductionOrder)
        self.assertEqual(self.stock(self.flour, self.wh), D("6"))
        self.assertEqual(self.stock(self.cake, self.wh), D("1"))

    def test_van_load_does_not_move_stock_twice(self):
        from apps.dispatch.models import VanLoad

        self.adjust(self.cake, "20")
        van = self.client.post(f"{API}/dispatch/vehicles/", {"name": "V"}, format="json").data
        body = {"vehicle": van["id"], "dispatch_at": "2026-03-05T06:30:00+04:00", "lines": [{"item": self.cake, "quantity": "5"}]}
        self.check("apps.dispatch.services", f"{API}/dispatch/loads/", body, VanLoad)
        self.assertEqual((self.stock(self.cake, self.wh), self.stock(self.cake, van["location"])), (D("15"), D("5")))

    def test_real_validation_errors_are_not_swallowed_by_the_retry(self):
        body = {"party": self.customer.id, "date": "2026-01-10", "items": [], "idempotency_key": "bad"}
        r = self.client.post(f"{API}/sales/invoices/", body, format="json")
        self.assertEqual(r.status_code, 400)


class GeneralVoucherMessageTests(StockBase):
    def test_general_voucher_on_receivable_without_party_gets_the_helpful_message(self):  # B13
        r = self.client.post(f"{API}/vouchers/", {"type": "general", "date": "2026-01-01", "lines": [
            {"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "general_control_account"))
        self.assertIn("Sales, Purchase, Receipt or Payment", r.data["detail"])

    def test_journal_still_requires_a_party_on_receivable_lines(self):
        r = self.client.post(f"{API}/vouchers/", {"type": "journal", "date": "2026-01-01", "lines": [
            {"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]}, format="json")
        self.assertEqual(r.data["code"], "party_required")

    def test_valid_general_voucher_still_posts(self):
        r = self.client.post(f"{API}/vouchers/", {"type": "general", "date": "2026-01-01", "lines": [
            {"account": self.acct("CASH").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]}, format="json")
        self.assertEqual(r.status_code, 201)
