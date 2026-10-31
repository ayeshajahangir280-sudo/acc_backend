from apps.inventory.models import StockMovement
from apps.organizations.models import Membership

from .base import AccountingTestCase, D

API = "/api/v1"


class StockBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.flour = self.item("FLOUR", "Flour", "raw_material", "kg")
        self.sugar = self.item("SUGAR", "Sugar", "raw_material", "kg")
        self.cake = self.item("CAKE", "Cake", "finished_good", "pcs")
        self.wh = self.client.get(f"{API}/inventory/locations/").data[0]["id"]  # default warehouse

    def item(self, sku, name, kind, unit="unit", client=None):
        r = (client or self.client).post(f"{API}/inventory/items/", {"sku": sku, "name": name, "kind": kind, "unit": unit}, format="json")
        assert r.status_code == 201, r.data
        return r.data["id"]

    def adjust(self, item, qty, type="opening", loc=None, client=None):
        body = {"item": item, "quantity": qty, "date": "2026-03-01", "type": type}
        if loc:
            body["location"] = loc
        return (client or self.client).post(f"{API}/inventory/adjustments/", body, format="json")

    def stock(self, item, loc):
        rows = self.client.get(f"{API}/inventory/stock/?item={item}").data["results"]
        return {r["location"]: D(r["quantity"]) for r in rows}.get(loc, D(0))


class InventoryTests(StockBase):
    def test_default_warehouse_exists_and_sku_unique_per_org(self):
        self.assertEqual(self.client.get(f"{API}/inventory/locations/").data[0]["name"], "Main Warehouse")
        dup = self.client.post(f"{API}/inventory/items/", {"sku": "FLOUR", "name": "x", "kind": "raw_material"}, format="json")
        self.assertEqual(dup.status_code, 400)
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.post(f"{API}/inventory/items/", {"sku": "FLOUR", "name": "Flour", "kind": "raw_material"}, format="json").status_code, 201)

    def test_adjustment_and_negative_stock_blocked(self):
        self.assertEqual(self.adjust(self.flour, "100").status_code, 201)
        self.assertEqual(self.stock(self.flour, self.wh), D("100"))
        bad = self.adjust(self.flour, "-150", "adjustment")
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "insufficient_stock"))
        self.assertEqual(self.stock(self.flour, self.wh), D("100"))
        self.assertEqual(self.adjust(self.flour, "-30", "adjustment").status_code, 201)
        self.assertEqual(self.stock(self.flour, self.wh), D("70"))
        self.assertEqual(self.adjust(self.flour, "0").status_code, 400)

    def test_production_consumes_raw_and_creates_finished_goods(self):
        self.adjust(self.flour, "100"); self.adjust(self.sugar, "50")
        r = self.client.post(f"{API}/inventory/production/", {"date": "2026-03-02", "item": self.cake, "quantity": "40",
                            "inputs": [{"item": self.flour, "quantity": "60"}, {"item": self.sugar, "quantity": "20"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["number"], "PRD-000001")
        self.assertEqual((self.stock(self.flour, self.wh), self.stock(self.sugar, self.wh), self.stock(self.cake, self.wh)),
                         (D("40"), D("30"), D("40")))
        self.assertEqual(len(r.data["inputs"]), 2)

    def test_insufficient_raw_material_rolls_everything_back(self):
        self.adjust(self.flour, "10"); self.adjust(self.sugar, "50")
        r = self.client.post(f"{API}/inventory/production/", {"date": "2026-03-02", "item": self.cake, "quantity": "5",
                            "inputs": [{"item": self.sugar, "quantity": "20"}, {"item": self.flour, "quantity": "11"}]}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "insufficient_stock"))
        self.assertEqual((self.stock(self.sugar, self.wh), self.stock(self.cake, self.wh)), (D("50"), D("0")))
        self.assertEqual(self.client.get(f"{API}/inventory/production/").data["count"], 0)

    def test_production_validation(self):
        self.adjust(self.flour, "100")

        def prod(**kw):
            body = {"date": "2026-03-02", "item": self.cake, "quantity": "1", "inputs": [{"item": self.flour, "quantity": "1"}], **kw}
            return self.client.post(f"{API}/inventory/production/", body, format="json")

        self.assertEqual(prod(item=self.flour).data["code"], "invalid_output_item")
        self.assertEqual(prod(inputs=[{"item": self.cake, "quantity": "1"}]).data["code"], "invalid_input_item")
        self.assertEqual(prod(inputs=[]).status_code, 400)
        self.assertEqual(prod(quantity="0").data["code"], "invalid_quantity")
        self.assertEqual(prod(inputs=[{"item": self.flour, "quantity": "1"}, {"item": self.flour, "quantity": "2"}]).data["code"], "duplicate_input")
        self.assertEqual(prod(inputs=[{"item": self.flour, "quantity": "-1"}]).data["code"], "invalid_quantity")

    def test_production_idempotent_and_void(self):
        self.adjust(self.flour, "100")
        body = {"date": "2026-03-02", "item": self.cake, "quantity": "10", "inputs": [{"item": self.flour, "quantity": "50"}], "idempotency_key": "p1"}
        a = self.client.post(f"{API}/inventory/production/", body, format="json")
        b = self.client.post(f"{API}/inventory/production/", body, format="json")
        self.assertEqual((a.status_code, b.status_code, a.data["id"] == b.data["id"]), (201, 200, True))
        self.assertEqual(self.stock(self.flour, self.wh), D("50"))
        self.assertEqual(self.client.post(f"{API}/inventory/production/{a.data['id']}/void/").status_code, 200)
        self.assertEqual((self.stock(self.flour, self.wh), self.stock(self.cake, self.wh)), (D("100"), D("0")))
        self.assertEqual(self.client.post(f"{API}/inventory/production/{a.data['id']}/void/").data["code"], "already_void")

    def test_cannot_void_production_when_goods_already_gone(self):
        self.adjust(self.flour, "100")
        o = self.client.post(f"{API}/inventory/production/", {"date": "2026-03-02", "item": self.cake, "quantity": "10",
                            "inputs": [{"item": self.flour, "quantity": "50"}]}, format="json").data
        self.adjust(self.cake, "-8", "adjustment")
        r = self.client.post(f"{API}/inventory/production/{o['id']}/void/")
        self.assertEqual(r.data["code"], "insufficient_stock")
        self.assertEqual(self.stock(self.flour, self.wh), D("50"))  # nothing half-reversed

    def test_movement_ledger_is_append_only_through_api(self):
        self.adjust(self.flour, "5")
        m = self.client.get(f"{API}/inventory/movements/").data["results"][0]
        self.assertEqual(self.client.delete(f"{API}/inventory/movements/{m['id']}/").status_code, 405)
        self.assertEqual(self.client.patch(f"{API}/inventory/movements/{m['id']}/", {"quantity": "9"}, format="json").status_code, 405)

    def test_item_kind_locked_after_movements(self):
        self.adjust(self.flour, "5")
        r = self.client.patch(f"{API}/inventory/items/{self.flour}/", {"kind": "finished_good"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_isolation_and_permissions(self):
        self.adjust(self.flour, "5")
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(f"{API}/inventory/items/").data["count"], 0)
        self.assertEqual(bob.get(f"{API}/inventory/stock/").data["count"], 0)
        self.assertEqual(bob.get(f"{API}/inventory/movements/").data["count"], 0)
        self.assertEqual(self.adjust(self.flour, "5", client=bob).status_code, 400)  # foreign item id
        r = bob.post(f"{API}/inventory/production/", {"date": "2026-03-02", "item": self.cake, "quantity": "1",
                     "inputs": [{"item": self.flour, "quantity": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        Membership.objects.create(user=self.bob, organization=self.bakery, role="viewer")
        viewer = self.as_user(self.bob, self.bakery)
        self.assertEqual(viewer.get(f"{API}/inventory/stock/").status_code, 200)
        self.assertEqual(self.adjust(self.flour, "1", client=viewer).status_code, 403)


class DispatchTests(StockBase):
    def setUp(self):
        super().setUp()
        self.adjust(self.cake, "100")
        self.van = self.client.post(f"{API}/dispatch/vehicles/", {"name": "Van 1", "plate_number": "A 1234", "driver_name": "Omar"}, format="json").data

    def load(self, qty="30", **kw):
        body = {"vehicle": self.van["id"], "dispatch_at": "2026-03-05T06:30:00+04:00", "lines": [{"item": self.cake, "quantity": qty}], **kw}
        return self.client.post(f"{API}/dispatch/loads/", body, format="json")

    def test_vehicle_gets_its_own_stock_location(self):
        locs = {l["name"]: l for l in self.client.get(f"{API}/inventory/locations/").data}
        self.assertEqual(locs["Van: Van 1"]["kind"], "vehicle")
        self.assertEqual(self.client.post(f"{API}/dispatch/vehicles/", {"name": "Van 1"}, format="json").status_code, 400)

    def test_loading_moves_stock_to_van(self):
        r = self.load("30")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["number"], r.data["status"], r.data["assigned_to"]), ("VL-000001", "loaded", "Omar"))
        self.assertEqual(self.stock(self.cake, self.wh), D("70"))
        self.assertEqual(self.stock(self.cake, self.van["location"]), D("30"))

    def test_cannot_load_more_than_available(self):
        r = self.load("150")
        self.assertEqual((r.status_code, r.data["code"]), (400, "insufficient_stock"))
        self.assertEqual(self.stock(self.cake, self.wh), D("100"))
        self.assertEqual(self.client.get(f"{API}/dispatch/loads/").data["count"], 0)

    def test_full_lifecycle_delivered_returned_remaining(self):
        lid = self.load("30").data["id"]
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{lid}/dispatch/").data["status"], "out_for_delivery")
        r = self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "20", "returned": "6"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        line = r.data["lines"][0]
        self.assertEqual((r.data["status"], D(line["quantity_delivered"]), D(line["quantity_returned"]), D(line["remaining"])),
                         ("closed", D("20"), D("6"), D("4")))
        self.assertEqual(self.stock(self.cake, self.wh), D("76"))                # 70 + 6 returned
        self.assertEqual(self.stock(self.cake, self.van["location"]), D("4"))    # remaining on the van
        types = {m["type"] for m in self.client.get(f"{API}/inventory/movements/?item={self.cake}").data["results"]}
        self.assertIn("van_delivered", types)

    def test_close_validation_and_no_double_close(self):
        lid = self.load("30").data["id"]
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": []}, format="json").data["code"], "invalid_transition")
        self.client.post(f"{API}/dispatch/loads/{lid}/dispatch/")
        over = self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "25", "returned": "10"}]}, format="json")
        self.assertEqual(over.data["code"], "over_closed")
        neg = self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "-1", "returned": "0"}]}, format="json")
        self.assertEqual(neg.status_code, 400)
        unknown = self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.flour, "delivered": "1", "returned": "0"}]}, format="json")
        self.assertEqual(unknown.data["code"], "unknown_item")
        self.assertEqual(self.stock(self.cake, self.van["location"]), D("30"))  # failed attempts moved nothing
        ok = self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "30", "returned": "0"}]}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": []}, format="json").data["code"], "invalid_transition")
        self.assertEqual(self.stock(self.cake, self.van["location"]), D("0"))

    def test_cancel_only_before_dispatch(self):
        lid = self.load("30").data["id"]
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{lid}/cancel/").data["status"], "cancelled")
        self.assertEqual((self.stock(self.cake, self.wh), self.stock(self.cake, self.van["location"])), (D("100"), D("0")))
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{lid}/cancel/").data["code"], "invalid_transition")
        l2 = self.load("10").data["id"]
        self.client.post(f"{API}/dispatch/loads/{l2}/dispatch/")
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{l2}/cancel/").data["code"], "invalid_transition")
        self.assertEqual(self.client.post(f"{API}/dispatch/loads/{l2}/dispatch/").data["code"], "invalid_transition")

    def test_idempotent_load_and_duplicate_lines(self):
        a = self.load("5", idempotency_key="L1"); b = self.load("5", idempotency_key="L1")
        self.assertEqual((a.status_code, b.status_code, a.data["id"] == b.data["id"]), (201, 200, True))
        self.assertEqual(self.stock(self.cake, self.van["location"]), D("5"))
        dup = self.client.post(f"{API}/dispatch/loads/", {"vehicle": self.van["id"], "dispatch_at": "2026-03-05T06:30:00+04:00",
                              "lines": [{"item": self.cake, "quantity": "1"}, {"item": self.cake, "quantity": "2"}]}, format="json")
        self.assertEqual(dup.data["code"], "duplicate_item")

    def test_inactive_vehicle_and_isolation(self):
        lid = self.load("5").data["id"]
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(f"{API}/dispatch/loads/").data["count"], 0)
        self.assertEqual(bob.get(f"{API}/dispatch/loads/{lid}/").status_code, 404)
        self.assertEqual(bob.post(f"{API}/dispatch/loads/{lid}/dispatch/").status_code, 404)
        r = bob.post(f"{API}/dispatch/loads/", {"vehicle": self.van["id"], "dispatch_at": "2026-03-05T06:30:00+04:00",
                     "lines": [{"item": self.cake, "quantity": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.client.patch(f"{API}/dispatch/vehicles/{self.van['id']}/", {"is_active": False}, format="json")
        self.assertEqual(self.load("1").data["code"], "invalid_vehicle")

    def test_dispatch_never_touches_accounting(self):
        from apps.ledger.models import JournalLine

        lid = self.load("10").data["id"]
        self.client.post(f"{API}/dispatch/loads/{lid}/dispatch/")
        self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "10", "returned": "0"}]}, format="json")
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_stock_conserved_across_production_and_dispatch(self):
        self.adjust(self.flour, "200")
        self.client.post(f"{API}/inventory/production/", {"date": "2026-03-02", "item": self.cake, "quantity": "50",
                         "inputs": [{"item": self.flour, "quantity": "100"}]}, format="json")
        lid = self.load("120").data["id"]
        self.client.post(f"{API}/dispatch/loads/{lid}/dispatch/")
        self.client.post(f"{API}/dispatch/loads/{lid}/close/", {"lines": [{"item": self.cake, "delivered": "100", "returned": "15"}]}, format="json")
        # 100 opening + 50 produced - 100 delivered = 50 still held: 45 warehouse, 5 left on the van
        self.assertEqual(sum(m.quantity for m in StockMovement.objects.filter(item_id=self.cake)), D("50"))
        held = self.client.get(f"{API}/inventory/stock/?item={self.cake}").data["results"]
        self.assertEqual(sum(D(r["quantity"]) for r in held), D("50"))
