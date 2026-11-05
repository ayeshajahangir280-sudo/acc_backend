"""Inventory costing (weighted average): valuation, COGS, returns, voids, adjustments, reconciliation to the ledger."""
import threading
from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from apps.inventory import costing
from apps.inventory.models import Item, Location, StockMovement, StockValuation
from apps.ledger.models import JournalLine, JournalTransaction
from apps.organizations.models import Branch, BranchAccess, Membership
from apps.parties.models import Party

from .base import AccountingTestCase, D

User = get_user_model()
SALES, PURCH = "/api/v1/sales/invoices/", "/api/v1/purchases/invoices/"
COST = "/api/v1/inventory/costing/"


class CostBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.flour = Item.objects.create(organization=self.bakery, sku="FL", name="Flour", kind="raw_material")
        self.bread = Item.objects.create(organization=self.bakery, sku="BR", name="Bread", kind="finished_good")

    def enable(self, costs=None, on="2026-01-01", client=None):
        return (client or self.client).post(COST + "enable/", {"date": on, "costs": costs or {}}, format="json")

    def line(self, item, qty, price, tax="0"):
        return {"description": item.name, "quantity": str(qty), "unit_price": str(price), "tax_rate": tax, "item": item.id}

    def buy(self, item, qty, price, on="2026-02-01", **kw):
        r = self.client.post(PURCH, {"party": self.supplier.id, "date": on, "items": [self.line(item, qty, price)], **kw}, format="json")
        return r

    def sell(self, item, qty, price="50", on="2026-03-01", client=None, **kw):
        return (client or self.client).post(SALES, {"party": self.customer.id, "date": on, "items": [self.line(item, qty, price)], **kw}, format="json")

    def ok(self, r):
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def bal(self, code):
        return self.balance(code)

    def recon(self):
        r = self.client.get(COST + "valuation/")
        self.assertEqual(r.status_code, 200, r.content)
        return r.data

    def assert_reconciled(self):
        rec = self.recon()
        self.assertEqual(D(str(rec["difference"])), D("0"), rec)
        self.assertEqual(rec["quantity_mismatches"], [])
        self.assertTrue(self.trial_balance_ok())
        return rec

    def counts(self):
        return (JournalTransaction.objects.count(), StockMovement.objects.count(), StockValuation.objects.count())


class EnableTests(CostBase):
    def test_items_on_lines_rejected_until_enabled(self):
        r = self.buy(self.flour, 1, 10)
        self.assertEqual(r.data["code"], "costing_disabled")
        self.assertEqual(self.client.get(COST).data["enabled"], False)

    def test_enable_values_existing_stock_and_posts_opening_entry(self):
        loc = Location.objects.create(organization=self.bakery, name="W")
        StockMovement.objects.create(organization=self.bakery, item=self.flour, location=loc, quantity=D("10"), type="opening", date=date(2025, 12, 1))
        self.assertEqual(self.enable().data["code"], "cost_required")
        r = self.enable({str(self.flour.id): "4.5"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(str(r.data["opening_value"])), D("45.00"))
        self.assertEqual(self.bal("INVENTORY"), D("45.00"))
        self.assert_reconciled()
        self.assertEqual(self.enable({str(self.flour.id): "4.5"}).data["code"], "already_enabled")

    def test_only_admins_enable_and_org_isolated(self):
        acct = User.objects.create_user("acc", password="pw-12345-xx")
        Membership.objects.create(user=acct, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        self.assertEqual(self.enable(client=self.as_user(acct, self.bakery)).status_code, 403)
        self.assertEqual(self.enable(costs={"999999": "1"}).data["code"], "invalid_reference")
        self.assertEqual(self.client.get(COST).data["enabled"], False)
        self.enable()
        self.assertEqual(self.as_user(self.bob, self.other).get(COST).data["enabled"], False)

    def test_enable_fails_in_locked_period_and_changes_nothing(self):
        loc = Location.objects.create(organization=self.bakery, name="W")
        StockMovement.objects.create(organization=self.bakery, item=self.flour, location=loc, quantity=D("1"), type="opening", date=date(2025, 12, 1))
        self.bakery.books_locked_through = date(2026, 1, 31)
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(self.enable({str(self.flour.id): "5"}).data["code"], "period_locked")
        self.assertEqual(self.client.get(COST).data["enabled"], False)
        self.assertEqual(StockValuation.objects.count(), 0)


class FlowTests(CostBase):
    def setUp(self):
        super().setUp()
        self.enable()

    def test_weighted_average_cogs_and_ledger(self):
        self.ok(self.buy(self.flour, 10, 10))
        self.ok(self.buy(self.flour, 10, 20, on="2026-02-05"))
        self.assertEqual((self.bal("INVENTORY"), self.bal("PURCHASES")), (D("300.00"), D("0")))
        inv = self.ok(self.sell(self.flour, 5))
        self.assertEqual(self.bal("COGS"), D("75.00"))          # 5 x average 15
        self.assertEqual(self.bal("INVENTORY"), D("225.00"))
        self.assertEqual(D(inv["items"][0]["stock_value"]), D("75.00"))
        rec = self.assert_reconciled()
        self.assertEqual((D(str(rec["items"][0]["quantity"])), D(str(rec["items"][0]["average_cost"]))), (D("15"), D("15.00")))

    def test_last_units_take_remaining_value_without_residue(self):
        self.ok(self.buy(self.flour, 3, "3.3333"))               # value 10.00 (3 x 3.3333 = 9.9999 -> 10.00)
        self.ok(self.sell(self.flour, 1))
        self.ok(self.sell(self.flour, 2, on="2026-03-02"))
        self.assertEqual(self.bal("INVENTORY"), D("0"))
        self.assert_reconciled()

    def test_insufficient_stock_blocks_sale_and_posts_nothing(self):
        self.ok(self.buy(self.flour, 2, 10))
        before = self.counts()
        r = self.sell(self.flour, 3)
        self.assertEqual(r.data["code"], "insufficient_stock")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.client.get(SALES).data["count"], 0)

    def test_sales_return_restores_stock_at_original_cost(self):
        self.ok(self.buy(self.flour, 10, 10))
        sale = self.ok(self.sell(self.flour, 4))                  # cogs 40
        self.ok(self.buy(self.flour, 10, 30, on="2026-03-05"))    # average moves on
        ret = lambda q, **k: self.client.post(SALES, {"party": self.customer.id, "date": "2026-03-10", "kind": "return", "original": sale["id"],
                                                      "items": [self.line(self.flour, q, 10)], **k}, format="json")
        self.assertEqual(ret(5).data["code"], "over_return_qty")
        r = ret(1)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(r.data["items"][0]["stock_value"]), D("10.00"))   # 40 / 4, not today's average
        self.assertEqual(self.bal("COGS"), D("30.00"))
        self.ok(ret(3, ))
        self.assertEqual(self.bal("COGS"), D("0"))
        self.assertEqual(ret(1).data["code"], "over_return_qty")
        self.assert_reconciled()

    def test_return_of_item_not_on_original(self):
        self.ok(self.buy(self.flour, 5, 10))
        self.ok(self.buy(self.bread, 5, 10))
        sale = self.ok(self.sell(self.flour, 1))
        r = self.client.post(SALES, {"party": self.customer.id, "date": "2026-03-10", "kind": "return", "original": sale["id"],
                                     "items": [self.line(self.bread, 1, 50)]}, format="json")
        self.assertEqual(r.data["code"], "item_not_on_original")

    def test_purchase_return_at_original_cost_with_price_variance(self):
        bill = self.ok(self.buy(self.flour, 10, 10))
        r = self.client.post(PURCH, {"party": self.supplier.id, "date": "2026-02-10", "kind": "return", "original": bill["id"],
                                     "items": [self.line(self.flour, 4, 12)]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.bal("INVENTORY"), D("60.00"))        # 40 out at original cost
        self.assertEqual(self.bal("PURCHASE_RETURNS"), D("-8.00"))  # 48 credit note less 40 cost = 8 credited
        self.assert_reconciled()

    def test_voids_reverse_stock_and_value(self):
        bill = self.ok(self.buy(self.flour, 10, 10))
        sale = self.ok(self.sell(self.flour, 6))
        self.assertEqual(self.client.post(f"{PURCH}{bill['id']}/void/").data["code"], "insufficient_stock")   # goods already sold
        self.assertEqual(self.bal("INVENTORY"), D("40.00"))                                                    # rolled back
        self.assertEqual(self.client.post(f"{SALES}{sale['id']}/void/").status_code, 200)
        self.assertEqual((self.bal("INVENTORY"), self.bal("COGS")), (D("100.00"), D("0")))
        self.assertEqual(self.client.post(f"{PURCH}{bill['id']}/void/").status_code, 200)
        self.assertEqual(self.bal("INVENTORY"), D("0"))
        self.assert_reconciled()

    def test_void_of_returns(self):
        bill = self.ok(self.buy(self.flour, 10, 10))
        sale = self.ok(self.sell(self.flour, 4))
        sr = self.ok(self.client.post(SALES, {"party": self.customer.id, "date": "2026-03-10", "kind": "return", "original": sale["id"],
                                              "items": [self.line(self.flour, 2, 50)]}, format="json"))
        pr = self.ok(self.client.post(PURCH, {"party": self.supplier.id, "date": "2026-03-11", "kind": "return", "original": bill["id"],
                                              "items": [self.line(self.flour, 3, 10)]}, format="json"))
        self.assertEqual(self.client.post(f"{SALES}{sr['id']}/void/").status_code, 200)
        self.assertEqual(self.client.post(f"{PURCH}{pr['id']}/void/").status_code, 200)
        self.assertEqual(self.bal("INVENTORY"), D("60.00"))
        self.assert_reconciled()

    def test_taxes_stay_out_of_inventory(self):
        r = self.client.post(PURCH, {"party": self.supplier.id, "date": "2026-02-01", "items": [self.line(self.flour, 10, 10, tax="10")]}, format="json")
        self.ok(r)
        self.assertEqual((self.bal("INVENTORY"), self.bal("TAX_INPUT")), (D("100.00"), D("10.00")))

    def test_mixed_lines_split_between_inventory_and_expense(self):
        r = self.client.post(PURCH, {"party": self.supplier.id, "date": "2026-02-01", "items": [self.line(self.flour, 2, 10), {"description": "Delivery", "quantity": "1", "unit_price": "5", "tax_rate": "0"}]}, format="json")
        self.ok(r)
        self.assertEqual((self.bal("INVENTORY"), self.bal("PURCHASES")), (D("20.00"), D("5.00")))

    def test_period_lock_and_foreign_items_roll_back(self):
        self.ok(self.buy(self.flour, 5, 10))
        before = self.counts()
        self.bakery.books_locked_through = date(2026, 3, 31)
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(self.sell(self.flour, 1, on="2026-03-10").data["code"], "period_locked")
        self.assertEqual(self.counts(), before)
        foreign = Item.objects.create(organization=self.other, sku="X", name="X", kind="raw_material")
        self.bakery.books_locked_through = None
        self.bakery.save(update_fields=["books_locked_through"])
        self.assertEqual(self.buy(foreign, 1, 1).data["code"], "invalid_reference")
        self.assertEqual(self.counts(), before)

    def test_branch_guard_rolls_back_stock(self):
        n = Branch.objects.create(organization=self.bakery, name="N")
        s = Branch.objects.create(organization=self.bakery, name="S")
        nora = User.objects.create_user("nora", password="pw-12345-xx")
        m = Membership.objects.create(user=nora, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        BranchAccess.objects.create(membership=m, branch=n)
        cn = self.as_user(nora, self.bakery)
        self.ok(self.buy(self.flour, 5, 10))
        before = self.counts()
        self.assertEqual(self.sell(self.flour, 1, client=cn, branch=s.id).data["code"], "branch_forbidden")
        self.assertEqual(self.counts(), before)
        self.ok(self.sell(self.flour, 1, client=cn, branch=n.id))

    def test_adjustments_are_valued_and_posted(self):
        adj = lambda q, **k: self.client.post("/api/v1/inventory/adjustments/", {"item": self.flour.id, "quantity": q, "date": "2026-02-01", **k}, format="json")
        self.assertEqual(adj("5").data["code"], "cost_required")
        self.assertEqual(adj("5", unit_cost="4").status_code, 201)
        self.assertEqual((self.bal("INVENTORY"), self.bal("STOCK_ADJUSTMENT")), (D("20.00"), D("-20.00")))
        self.assertEqual(adj("-2").status_code, 201)                        # at average cost 4
        self.assertEqual((self.bal("INVENTORY"), self.bal("STOCK_ADJUSTMENT")), (D("12.00"), D("-12.00")))
        self.assertEqual(adj("-9").data["code"], "insufficient_stock")
        self.assert_reconciled()

    def test_transfers_allowed_production_and_van_blocked(self):
        self.ok(self.buy(self.flour, 5, 10))
        w2 = Location.objects.create(organization=self.bakery, name="W2")
        from apps.inventory import services as st
        st.transfer_stock(self.bakery, self.alice, item=self.flour, source=st.default_location(self.bakery), destination=w2, quantity=2, date=date(2026, 3, 1))
        self.assert_reconciled()
        r = self.client.post("/api/v1/inventory/production/", {"date": "2026-03-01", "item": self.bread.id, "quantity": "1",
                                                              "inputs": [{"item": self.flour.id, "quantity": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "costing_unsupported")
        self.assert_reconciled()


class CostingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization

        self.user = User.objects.create_user("cc", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race Cost")
        self.cust = Party.objects.create(organization=self.org, name="C", kind="customer")
        self.sup = Party.objects.create(organization=self.org, name="S", kind="supplier")
        self.item = Item.objects.create(organization=self.org, sku="I", name="I", kind="raw_material")
        costing.enable_costing(self.org, self.user, date=date(2026, 1, 1), costs={})

    def _api(self):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(self.user)
        c.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return c

    def _race(self, fns):
        barrier, out = threading.Barrier(len(fns)), []

        def go(fn):
            try:
                barrier.wait()
                out.append(fn())
            finally:
                connection.close()
        ts = [threading.Thread(target=go, args=(f,)) for f in fns]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return out

    def test_concurrent_sales_cannot_oversell_and_ledger_reconciles(self):
        line = lambda q: {"description": "i", "quantity": q, "unit_price": "10", "item": self.item.id}
        self.assertEqual(self._api().post(PURCH, {"party": self.sup.id, "date": "2026-02-01", "items": [line("10")]}, format="json").status_code, 201)
        sell = lambda: self._api().post(SALES, {"party": self.cust.id, "date": "2026-03-01", "items": [line("6")]}, format="json").status_code
        codes = self._race([sell, sell, sell])
        self.assertEqual(sorted(codes), [201, 400, 400])
        rec = costing.reconcile(self.org)
        self.assertEqual(rec["difference"], 0)
        self.assertEqual(rec["quantity_mismatches"], [])
        self.assertEqual(rec["items"][0]["quantity"], 4)

    def test_sale_racing_purchase_void_stays_consistent(self):
        line = lambda q: {"description": "i", "quantity": q, "unit_price": "10", "item": self.item.id}
        for _ in range(3):
            bill = self._api().post(PURCH, {"party": self.sup.id, "date": "2026-02-01", "items": [line("5")]}, format="json").data
            sell = lambda: self._api().post(SALES, {"party": self.cust.id, "date": "2026-03-01", "items": [line("5")]}, format="json").status_code
            void = lambda: self._api().post(f"{PURCH}{bill['id']}/void/").status_code
            self._race([sell, void])
            rec = costing.reconcile(self.org)
            self.assertEqual(rec["difference"], 0)
            self.assertEqual(rec["quantity_mismatches"], [])
            self.assertGreaterEqual(rec["items"][0]["quantity"], 0)
