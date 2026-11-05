"""Order lines linked to stock items: costing rules apply on conversion, partial conversions move partial stock."""
from apps.inventory.models import StockValuation

from .test_costing import COST, CostBase, D, PURCH, SALES

SO, PO = "/api/v1/sales/orders/", "/api/v1/purchases/orders/"


class OrderStockTests(CostBase):
    def order(self, item, qty="10", price="50", url=SO, **kw):
        body = {"party": (self.customer if url == SO else self.supplier).id, "date": "2026-03-01",
                "items": [{"description": item.name, "quantity": qty, "unit_price": price, "tax_rate": "0", "item": item.id}], **kw}
        if url == SO:
            body["kind"] = "order"
        return self.client.post(url, body, format="json")

    def test_item_link_needs_costing_and_a_valid_item(self):
        r = self.order(self.bread)
        self.assertEqual(r.data["code"], "costing_disabled")
        self.ok(self.enable())
        self.ok(self.buy(self.bread, 20, 10))
        self.assertEqual(self.order(self.bread).status_code, 201)
        foreign = self.bread.__class__.objects.create(organization=self.other, sku="X", name="X", kind="finished_good")
        r = self.order(foreign)
        self.assertEqual(r.data["code"], "invalid_reference")
        self.bread.is_active = False
        self.bread.save()
        self.assertEqual(self.order(self.bread).data["code"], "invalid_reference")

    def test_partial_conversions_move_partial_stock_at_average_cost(self):
        self.ok(self.enable())
        self.ok(self.buy(self.bread, 20, 10))
        o = self.order(self.bread).data
        line = o["items"][0]["id"]
        self.assertEqual(o["items"][0]["item"], self.bread.id)
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice", "idempotency_key": "p1", "lines": [{"item": line, "quantity": "4"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal("INVENTORY"), D("200") - D("40"))       # 4 units out at 10
        self.assertEqual(self.bal("COGS"), D("40"))
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json")                # the remaining 6
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal("INVENTORY"), D("100"))
        self.assertEqual(self.client.get(f"{SO}{o['id']}/").data["status"], "converted")
        self.assert_reconciled()

    def test_insufficient_stock_blocks_conversion_atomically(self):
        self.ok(self.enable())
        self.ok(self.buy(self.bread, 3, 10))
        o = self.order(self.bread, qty="5").data
        r = self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(r.data["code"], "insufficient_stock")
        d = self.client.get(f"{SO}{o['id']}/").data
        self.assertEqual((d["status"], d["items"][0]["invoiced_qty"]), ("open", "0.0000"))
        self.assert_reconciled()

    def test_purchase_order_conversion_receives_stock_into_inventory(self):
        self.ok(self.enable())
        o = self.order(self.flour, qty="100", price="2", url=PO).data
        r = self.client.post(f"{PO}{o['id']}/convert/", {"to": "invoice", "idempotency_key": "b1",
                             "lines": [{"item": o["items"][0]["id"], "quantity": "60"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal("INVENTORY"), D("120"))
        self.assertEqual(sum(v.quantity for v in StockValuation.objects.filter(item=self.flour)), D("60"))
        self.assert_reconciled()

    def test_quote_to_order_keeps_item_link(self):
        self.ok(self.enable())
        q = self.client.post(SO, {"party": self.customer.id, "date": "2026-03-01", "kind": "quote", "items": [
            {"description": "Bread", "quantity": "2", "unit_price": "5", "item": self.bread.id}]}, format="json").data
        o = self.client.post(f"{SO}{q['id']}/convert/", {"to": "order"}, format="json").data
        self.assertEqual(o["items"][0]["item"], self.bread.id)

    def test_billed_order_cannot_be_edited(self):
        self.ok(self.enable())
        self.ok(self.buy(self.bread, 20, 10))
        o = self.order(self.bread).data
        self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice", "idempotency_key": "e1", "lines": [{"item": o["items"][0]["id"], "quantity": "1"}]}, format="json")
        r = self.client.patch(f"{SO}{o['id']}/", {"items": [{"description": "x", "quantity": "1", "unit_price": "1", "item": self.bread.id}]}, format="json")
        self.assertEqual(r.data["code"], "not_editable")
