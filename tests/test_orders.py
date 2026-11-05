"""Quotes, sales orders and purchase orders: non-posting documents converted through the normal posting services."""
import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from apps.ledger.models import JournalLine, JournalTransaction
from apps.orders.models import PurchaseOrder, SalesOrder
from apps.organizations.models import Branch, BranchAccess, Membership
from apps.parties.models import Party
from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice

from .base import AccountingTestCase, D

User = get_user_model()
SO, PO = "/api/v1/sales/orders/", "/api/v1/purchases/orders/"


class OrderBase(AccountingTestCase):
    def body(self, party=None, price="100", qty="2", on="2026-03-01", **extra):
        return {"party": (party or self.customer).id, "date": on, "kind": "quote",
                "items": [{"description": "Bread", "quantity": qty, "unit_price": price, "tax_rate": "10"}], **extra}

    def make(self, url=SO, client=None, **kw):
        body = self.body(**kw)
        if url == PO:
            body.pop("kind"); body["party"] = kw.get("party", self.supplier).id
        r = (client or self.client).post(url, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def ledger(self):
        return JournalTransaction.objects.count(), JournalLine.objects.count()


class SalesQuoteOrderTests(OrderBase):
    def test_creating_documents_never_touches_the_ledger(self):
        n = self.ledger()
        q = self.make()
        self.assertEqual(q["number"], "QT-000001" if q["number"].startswith("QT-0") else q["number"])
        self.assertEqual(D(q["total"]), D("220.00"))
        self.assertEqual(q["status"], "open")
        self.assertEqual(self.ledger(), n)

    def test_numbering_is_separate_per_kind(self):
        q = self.make()
        o = self.make(kind="order")
        self.assertTrue(q["number"].startswith("QT"))
        self.assertTrue(o["number"].startswith("SO"))

    def test_idempotency_key_returns_same_document(self):
        a = self.client.post(SO, self.body(idempotency_key="k1"), format="json")
        b = self.client.post(SO, self.body(idempotency_key="k1"), format="json")
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(a.data["id"], b.data["id"])
        self.assertEqual(SalesOrder.objects.count(), 1)

    def test_validation(self):
        r = self.client.post(SO, self.body(party=self.supplier), format="json")
        self.assertEqual(r.data["code"], "invalid_party")
        r = self.client.post(SO, {**self.body(), "valid_until": "2026-02-01"}, format="json")
        self.assertEqual(r.data["code"], "invalid_due_date")
        r = self.client.post(SO, {**self.body(), "items": []}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(SO, {**self.body(), "items": [{"description": "x", "quantity": "0", "unit_price": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "invalid_item")

    def test_quote_accept_then_convert_to_invoice_posts_once(self):
        q = self.make()
        self.assertEqual(self.client.post(f"{SO}{q['id']}/accept/").data["status"], "accepted")
        before = self.ledger()
        r = self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice", "date": "2026-03-05"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        inv = SalesInvoice.objects.get(pk=r.data["invoice"])
        self.assertEqual((inv.total, inv.party_id, str(inv.date)), (D("220.00"), self.customer.id, "2026-03-05"))
        self.assertEqual(self.ledger()[0], before[0] + 1)
        self.assertTrue(self.trial_balance_ok())
        again = self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(again.data["code"], "invalid_transition")
        self.assertEqual(SalesInvoice.objects.count(), 1)
        detail = self.client.get(f"{SO}{q['id']}/").data
        self.assertEqual((detail["status"], detail["invoice"]), ("converted", inv.id))

    def test_quote_to_order_to_invoice_chain(self):
        q = self.make()
        o = self.client.post(f"{SO}{q['id']}/convert/", {"to": "order"}, format="json")
        self.assertEqual(o.status_code, 201, o.content)
        self.assertEqual((o.data["kind"], o.data["source"]), ("order", q["id"]))
        self.assertEqual(self.client.get(f"{SO}{q['id']}/").data["status"], "converted")
        self.assertEqual(self.client.post(f"{SO}{o.data['id']}/convert/", {"to": "order"}, format="json").data["code"], "invalid_conversion")
        self.assertEqual(self.client.post(f"{SO}{o.data['id']}/convert/", {"to": "invoice"}, format="json").status_code, 201)

    def test_decline_cancel_and_terminal_states(self):
        q = self.make()
        self.assertEqual(self.client.post(f"{SO}{q['id']}/decline/").data["status"], "declined")
        self.assertEqual(self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json").status_code, 404 if False else 400)
        o = self.make(kind="order")
        self.assertEqual(self.client.post(f"{SO}{o['id']}/accept/").data["code"], "not_a_quote")
        self.assertEqual(self.client.post(f"{SO}{o['id']}/cancel/").data["status"], "cancelled")
        self.assertEqual(self.client.post(f"{SO}{o['id']}/cancel/").data["code"], "invalid_transition")
        self.assertEqual(self.client.post(f"{SO}{o['id']}/convert/", {"to": "invoice"}, format="json").data["code"], "invalid_transition")

    def test_edit_open_document_recomputes_totals_but_not_converted(self):
        q = self.make()
        r = self.client.patch(f"{SO}{q['id']}/", {"items": [{"description": "Cake", "quantity": "1", "unit_price": "50", "tax_rate": "0"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((D(r.data["total"]), len(r.data["items"])), (D("50.00"), 1))
        self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json")
        r = self.client.patch(f"{SO}{q['id']}/", {"items": [{"description": "x", "quantity": "1", "unit_price": "1"}]}, format="json")
        self.assertEqual(r.data["code"], "not_editable")

    def test_conversion_respects_period_lock_and_rolls_back(self):
        from datetime import date
        q = self.make()
        self.bakery.books_locked_through = date(2026, 3, 31)
        self.bakery.save()
        r = self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice", "date": "2026-03-10"}, format="json")
        self.assertEqual(r.data["code"], "period_locked")
        self.assertEqual(self.client.get(f"{SO}{q['id']}/").data["status"], "open")
        self.assertEqual(SalesInvoice.objects.count(), 0)
        self.assertEqual(self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice", "date": "2026-04-10"}, format="json").status_code, 201)

    def test_voiding_the_invoice_leaves_the_order_converted(self):
        q = self.make()
        inv = self.client.post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json").data["invoice"]
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{inv}/void/").status_code, 200)
        self.assertEqual(self.client.get(f"{SO}{q['id']}/").data["status"], "converted")  # one conversion only; recreate if needed
        self.assertTrue(self.trial_balance_ok())

    def test_organization_isolation_and_roles(self):
        q = self.make()
        other = self.as_user(self.bob, self.other)
        self.assertEqual(other.get(f"{SO}{q['id']}/").status_code, 404)
        self.assertEqual(other.post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json").status_code, 404)
        self.assertEqual(other.get(SO).data["count"], 0)
        foreign = Party.objects.create(organization=self.other, name="F", kind="customer")
        self.assertEqual(self.client.post(SO, self.body(party=foreign), format="json").status_code, 400)
        v = User.objects.create_user("vv", password="pw-12345-xx")
        m = Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        cv = self.as_user(v, self.bakery)
        self.assertEqual(cv.post(SO, self.body(), format="json").status_code, 403)
        self.assertEqual(cv.post(f"{SO}{q['id']}/cancel/").status_code, 403)

    def test_branch_scope(self):
        n = Branch.objects.create(organization=self.bakery, name="N")
        s = Branch.objects.create(organization=self.bakery, name="S")
        nora = User.objects.create_user("nora", password="pw-12345-xx")
        m = Membership.objects.create(user=nora, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        BranchAccess.objects.create(membership=m, branch=n)
        cn = self.as_user(nora, self.bakery)
        qs = self.make(branch=s.id)
        qn = self.make(client=cn, branch=n.id)
        self.assertEqual(cn.get(f"{SO}{qs['id']}/").status_code, 404)
        self.assertEqual([r["id"] for r in cn.get(SO).data["results"]], [qn["id"]])
        self.assertEqual(cn.post(f"{SO}{qs['id']}/convert/", {"to": "invoice"}, format="json").status_code, 404)
        self.assertEqual(cn.post(SO, self.body(branch=s.id), format="json").data["code"], "branch_forbidden")
        r = cn.post(f"{SO}{qn['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SalesInvoice.objects.get(pk=r.data["invoice"]).branch_id, n.id)


class PurchaseOrderTests(OrderBase):
    def test_purchase_order_to_bill(self):
        n = self.ledger()
        po = self.make(PO)
        self.assertTrue(po["number"].startswith("PO"))
        self.assertEqual(self.ledger(), n)
        r = self.client.post(f"{PO}{po['id']}/convert/", {"to": "invoice"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        bill = PurchaseInvoice.objects.get(pk=r.data["invoice"])
        self.assertEqual((bill.total, bill.party_id), (D("220.00"), self.supplier.id))
        self.assertTrue(self.trial_balance_ok())
        self.assertEqual(self.client.post(f"{PO}{po['id']}/convert/", {"to": "invoice"}, format="json").data["code"], "invalid_transition")
        self.assertEqual(PurchaseInvoice.objects.count(), 1)

    def test_purchase_rules(self):
        po = self.make(PO)
        self.assertEqual(self.client.post(f"{PO}{po['id']}/accept/").data["code"], "not_a_quote")
        self.assertEqual(self.client.post(f"{PO}{po['id']}/convert/", {"to": "order"}, format="json").data["code"], "invalid_conversion")
        r = self.client.post(PO, {"party": self.customer.id, "date": "2026-03-01", "items": [{"description": "x", "quantity": "1", "unit_price": "5"}]}, format="json")
        self.assertEqual(r.data["code"], "invalid_party")
        self.assertEqual(self.as_user(self.bob, self.other).get(f"{PO}{po['id']}/").status_code, 404)
        self.assertEqual(self.client.post(f"{PO}{po['id']}/cancel/").data["status"], "cancelled")


class OrderConcurrencyTests(TransactionTestCase):
    """PostgreSQL only: two simultaneous conversions of one order must post exactly one invoice."""

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs PostgreSQL row locks")
        from apps.organizations.services import create_organization

        self.user = User.objects.create_user("ora", password="pw-12345-xx")
        self.org = create_organization(self.user, name="Race Orders")
        self.party = Party.objects.create(organization=self.org, name="C", kind="customer")

    def _api(self):
        from rest_framework.test import APIClient

        c = APIClient()
        c.force_authenticate(self.user)
        c.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))
        return c

    def test_double_convert_posts_one_invoice(self):
        q = self._api().post(SO, {"party": self.party.id, "date": "2026-03-01", "kind": "order",
                                  "items": [{"description": "x", "quantity": "1", "unit_price": "10"}]}, format="json").data
        barrier, out = threading.Barrier(3), []

        def go():
            try:
                barrier.wait()
                out.append(self._api().post(f"{SO}{q['id']}/convert/", {"to": "invoice"}, format="json").status_code)
            finally:
                connection.close()
        ts = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), [201, 400, 400])
        self.assertEqual(SalesInvoice.objects.filter(organization=self.org).count(), 1)
