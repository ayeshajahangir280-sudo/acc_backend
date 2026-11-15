"""Read-only `payment_status` filter on the sales invoice list (paid / pending / overdue / open)."""
from datetime import timedelta

from django.utils import timezone

from .base import AccountingTestCase

URL = "/api/v1/sales/invoices/"


class PaymentStatusFilterTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.future, self.past = (today + timedelta(days=10)).isoformat(), (today - timedelta(days=10)).isoformat()
        day = (today - timedelta(days=20)).isoformat()
        mk = lambda due, price: self.sale(qty="1", price=price, tax="0", date=day, due_date=due).data["id"]
        self.paid = mk(self.future, "100")
        self.partial_future = mk(self.future, "100")
        self.partial_past = mk(self.past, "100")
        self.unpaid_past = mk(self.past, "50")
        self.unpaid_future = mk(self.future, "50")
        self.voided = mk(self.future, "70")
        self.returned = mk(self.past, "40")  # fully returned, never paid => settled, not paid
        self.paid_then_returned = mk(self.past, "100")  # 60 paid + 40 returned => paid
        self.client.post(f"{URL}{self.voided}/void/")
        for orig, amt in ((self.returned, "40"), (self.paid_then_returned, "40")):
            r = self.client.post(URL, {"kind": "return", "original": orig, "party": self.customer.id, "date": day,
                                       "items": [{"description": "x", "quantity": "1", "unit_price": amt, "tax_rate": "0"}]}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        r = self.client.post("/api/v1/receipts/", {
            "party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "220", "date": day,
            "allocations": [{"invoice": self.paid, "amount": "100"}, {"invoice": self.partial_future, "amount": "30"},
                            {"invoice": self.partial_past, "amount": "30"}, {"invoice": self.paid_then_returned, "amount": "60"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def ids(self, status, **q):
        r = self.client.get(URL, {"kind": "invoice", "payment_status": status, **q})
        self.assertEqual(r.status_code, 200, r.content)
        return {row["id"] for row in r.data["results"]}

    def test_classification(self):
        self.assertEqual(self.ids("paid"), {self.paid, self.paid_then_returned})
        self.assertEqual(self.ids("settled"), {self.returned})
        self.assertEqual(self.ids("pending"), {self.partial_future, self.unpaid_future})
        self.assertEqual(self.ids("overdue"), {self.partial_past, self.unpaid_past})
        self.assertEqual(self.ids("open"), {self.partial_future, self.unpaid_future, self.partial_past, self.unpaid_past})

    def test_void_never_paid_and_unfiltered_list_unchanged(self):
        for s in ("paid", "settled", "pending", "overdue", "open"):
            self.assertNotIn(self.voided, self.ids(s))
        self.assertEqual(self.client.get(URL, {"kind": "invoice"}).data["count"], 8)

    def test_combines_with_other_filters_and_rejects_unknown(self):
        self.assertEqual(self.ids("paid", search="nomatch-xyz"), set())
        self.assertEqual(self.client.get(URL, {"payment_status": "bogus"}).status_code, 400)

    def test_statuses_partition_posted_invoices(self):
        parts = [self.ids(s) for s in ("paid", "settled", "pending", "overdue")]
        self.assertEqual(sum(len(p) for p in parts), len(set().union(*parts)))  # disjoint
        self.assertEqual(set().union(*parts), self.ids("paid") | self.ids("settled") | self.ids("open"))
        self.assertEqual(len(set().union(*parts)), 7)  # every posted invoice, void excluded

    def test_organization_isolation(self):
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(URL, {"payment_status": "open"}).data["count"], 0)
