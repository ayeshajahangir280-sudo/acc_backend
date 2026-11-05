"""Partial conversion of orders into several invoices/bills with per-line quantity tracking."""
import threading
import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from apps.orders.models import SalesOrder
from apps.organizations.models import Membership
from apps.parties.models import Party
from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice

from .base import AccountingTestCase, D
from .test_orders import PO, SO, OrderBase

User = get_user_model()


class PartialSalesTests(OrderBase):
    def order(self, **kw):
        body = {"party": self.customer.id, "date": "2026-03-01", "kind": "order", "items": [
            {"description": "Bread", "quantity": "10", "unit_price": "10", "tax_rate": "10"},
            {"description": "Cake", "quantity": "4", "unit_price": "50", "tax_rate": "0"}], **kw}
        r = self.client.post(SO, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def conv(self, o, lines, key=None, url=SO, client=None):
        return (client or self.client).post(f"{url}{o['id']}/convert/", {"to": "invoice", "lines": lines,
                                            "idempotency_key": key or uuid.uuid4().hex}, format="json")

    def test_two_partial_invoices_then_status_converted(self):
        o = self.order()
        bread, cake = o["items"][0]["id"], o["items"][1]["id"]
        r1 = self.conv(o, [{"item": bread, "quantity": "4"}])
        self.assertEqual(r1.status_code, 201, r1.content)
        inv1 = SalesInvoice.objects.get(pk=r1.data["invoice"])
        self.assertEqual(inv1.total, D("44.00"))  # 4 x 10 + 10% VAT
        d = self.client.get(f"{SO}{o['id']}/").data
        self.assertEqual(d["status"], "open")
        self.assertEqual([i["remaining_qty"] for i in d["items"]], ["6.0000", "4.0000"])
        r2 = self.conv(o, [{"item": bread, "quantity": "6"}, {"item": cake, "quantity": "4"}])
        self.assertEqual(r2.status_code, 201, r2.content)
        d = self.client.get(f"{SO}{o['id']}/").data
        self.assertEqual(d["status"], "converted")
        self.assertEqual(len(d["invoices"]), 2)
        self.assertEqual(d["invoice"], inv1.id)
        total = sum(SalesInvoice.objects.get(pk=r.data["invoice"]).total for r in (r1, r2))
        self.assertEqual(total, D(o["total"]))  # invoiced parts add up to the order
        self.assertTrue(self.trial_balance_ok())

    def test_over_conversion_and_bad_lines_rejected_without_side_effects(self):
        o = self.order()
        bread = o["items"][0]["id"]
        self.assertEqual(self.conv(o, [{"item": bread, "quantity": "4"}]).status_code, 201)
        n = SalesInvoice.objects.count()
        for lines, code in (([{"item": bread, "quantity": "7"}], "over_convert"),
                            ([{"item": bread, "quantity": "0"}], "invalid_quantity"),
                            ([{"item": bread, "quantity": "1"}, {"item": bread, "quantity": "1"}], "duplicate_line"),
                            ([{"item": 999999, "quantity": "1"}], "line_not_on_order"),
                            ([], "no_lines")):
            r = self.conv(o, lines)
            self.assertEqual(r.status_code, 400, r.content)
            self.assertEqual(r.data["code"], code)
        self.assertEqual(SalesInvoice.objects.count(), n)
        self.assertEqual(self.client.get(f"{SO}{o['id']}/").data["items"][0]["invoiced_qty"], "4.0000")

    def test_line_from_another_order_rejected(self):
        a, b = self.order(), self.order()
        r = self.conv(a, [{"item": b["items"][0]["id"], "quantity": "1"}])
        self.assertEqual(r.data["code"], "line_not_on_order")

    def test_partial_needs_idempotency_key_and_replay_is_harmless(self):
        o = self.order()
        bread = o["items"][0]["id"]
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice", "lines": [{"item": bread, "quantity": "2"}]}, format="json")
        self.assertEqual(r.data["code"], "idempotency_required")
        key = "same-click"
        first = self.conv(o, [{"item": bread, "quantity": "2"}], key=key)
        again = self.conv(o, [{"item": bread, "quantity": "2"}], key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.data["invoice"], first.data["invoice"])
        self.assertEqual(SalesInvoice.objects.count(), 1)
        self.assertEqual(self.client.get(f"{SO}{o['id']}/").data["items"][0]["invoiced_qty"], "2.0000")

    def test_whole_conversion_after_partial_invoices_only_the_remainder(self):
        o = self.order()
        bread = o["items"][0]["id"]
        self.conv(o, [{"item": bread, "quantity": "4"}])
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        inv = SalesInvoice.objects.get(pk=r.data["invoice"])
        self.assertEqual(inv.total, D("66.00") + D("200.00"))  # 6 x 10 + 10% and 4 x 50
        self.assertEqual(self.client.get(f"{SO}{o['id']}/").data["status"], "converted")
        again = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_partially_invoiced_order_cannot_be_edited_but_can_be_cancelled(self):
        o = self.order()
        self.conv(o, [{"item": o["items"][0]["id"], "quantity": "1"}])
        r = self.client.patch(f"{SO}{o['id']}/", {"items": [{"description": "x", "quantity": "1", "unit_price": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "not_editable")
        self.assertEqual(self.client.post(f"{SO}{o['id']}/cancel/").status_code, 200)
        self.assertEqual(self.conv(o, [{"item": o["items"][1]["id"], "quantity": "1"}]).status_code, 400)

    def test_period_lock_rolls_everything_back(self):
        o = self.order()
        from datetime import date
        self.bakery.books_locked_through = date(2026, 3, 31)
        self.bakery.save(update_fields=["books_locked_through"])
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice", "date": "2026-03-10", "idempotency_key": "k1",
                                                          "lines": [{"item": o["items"][0]["id"], "quantity": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "period_locked")
        self.assertEqual(self.client.get(f"{SO}{o['id']}/").data["items"][0]["invoiced_qty"], "0.0000")
        self.assertEqual(SalesInvoice.objects.count(), 0)

    def test_quote_to_order_cannot_be_partial(self):
        q = self.client.post(SO, self.body(), format="json").data
        r = self.client.post(f"{SO}{q['id']}/convert/", {"to": "order", "idempotency_key": "x",
                                                         "lines": [{"item": q["items"][0]["id"], "quantity": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "invalid_conversion")

    def test_viewer_and_other_org_blocked(self):
        o = self.order()
        v = User.objects.create_user("vw", password="pw-view-1234")
        Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        r = self.conv(o, [{"item": o["items"][0]["id"], "quantity": "1"}], client=self.as_user(v, self.bakery))
        self.assertEqual(r.status_code, 403)
        r = self.conv(o, [{"item": o["items"][0]["id"], "quantity": "1"}], client=self.as_user(self.bob, self.other))
        self.assertEqual(r.status_code, 404)

    def test_purchase_order_partial_bills(self):
        r = self.client.post(PO, {"party": self.supplier.id, "date": "2026-03-01", "items": [
            {"description": "Flour", "quantity": "100", "unit_price": "2", "tax_rate": "0"}]}, format="json")
        o = r.data
        r1 = self.conv(o, [{"item": o["items"][0]["id"], "quantity": "60"}], url=PO)
        self.assertEqual(r1.status_code, 201, r1.content)
        self.assertEqual(PurchaseInvoice.objects.get(pk=r1.data["invoice"]).total, D("120.00"))
        r2 = self.conv(o, [{"item": o["items"][0]["id"], "quantity": "41"}], url=PO)
        self.assertEqual(r2.data["code"], "over_convert")
        r3 = self.conv(o, [{"item": o["items"][0]["id"], "quantity": "40"}], url=PO)
        self.assertEqual(r3.status_code, 201)
        self.assertEqual(self.client.get(f"{PO}{o['id']}/").data["status"], "converted")

    def test_backfill_for_orders_converted_before_the_feature(self):
        import importlib

        o = self.order()
        self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json")
        from apps.orders.models import SalesOrderInvoice, SalesOrderItem

        # simulate the pre-migration state, then run the backfill
        SalesOrderInvoice.objects.all().delete()
        SalesOrderItem.objects.update(invoiced_qty=0)
        from django.apps import apps

        mig = importlib.import_module("apps.orders.migrations.0002_partial_conversion")
        mig.backfill(apps, None)
        d = self.client.get(f"{SO}{o['id']}/").data
        self.assertEqual([i["remaining_qty"] for i in d["items"]], ["0.0000", "0.0000"])
        self.assertEqual(len(d["invoices"]), 1)


class PartialConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization

        self.user = User.objects.create_user("pc", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race Partial")
        self.party = Party.objects.create(organization=self.org, name="C", kind="customer")

    def _api(self):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(self.user)
        c.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return c

    def test_racing_partial_conversions_never_over_invoice(self):
        o = self._api().post(SO, {"party": self.party.id, "date": "2026-03-01", "kind": "order",
                                  "items": [{"description": "x", "quantity": "10", "unit_price": "10"}]}, format="json").data
        item = o["items"][0]["id"]
        barrier, out = threading.Barrier(4), []

        def go(i):
            try:
                barrier.wait()
                r = self._api().post(f"{SO}{o['id']}/convert/", {"to": "invoice", "idempotency_key": f"k{i}",
                                     "lines": [{"item": item, "quantity": "4"}]}, format="json")
                out.append(r.status_code)
            finally:
                connection.close()
        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), [201, 201, 400, 400])  # 4 + 4 fit into 10; the third and fourth do not
        self.assertEqual(SalesInvoice.objects.filter(organization=self.org).count(), 2)
        d = self._api().get(f"{SO}{o['id']}/").data
        self.assertEqual(d["items"][0]["invoiced_qty"], "8.0000")

    def test_same_key_racing_creates_one_invoice(self):
        o = self._api().post(SO, {"party": self.party.id, "date": "2026-03-01", "kind": "order",
                                  "items": [{"description": "x", "quantity": "10", "unit_price": "10"}]}, format="json").data
        item = o["items"][0]["id"]
        barrier, out = threading.Barrier(3), []

        def go():
            try:
                barrier.wait()
                r = self._api().post(f"{SO}{o['id']}/convert/", {"to": "invoice", "idempotency_key": "dup",
                                     "lines": [{"item": item, "quantity": "3"}]}, format="json")
                out.append((r.status_code, r.data.get("invoice")))
            finally:
                connection.close()
        ts = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len({inv for _, inv in out}), 1, out)
        self.assertEqual(SalesInvoice.objects.filter(organization=self.org).count(), 1)
        self.assertEqual(self._api().get(f"{SO}{o['id']}/").data["items"][0]["invoiced_qty"], "3.0000")
