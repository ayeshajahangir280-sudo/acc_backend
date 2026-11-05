"""Chart-of-accounts hierarchy: validation, isolation, rollup, posting to parents, concurrency."""
import threading

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from apps.ledger.models import Account
from apps.ledger import hierarchy
from apps.organizations.models import Membership
from apps.organizations.services import create_organization
from apps.core.errors import DomainError

from .base import AccountingTestCase, D

URL = "/api/v1/accounts/"


class SubAccountTests(AccountingTestCase):
    def mk(self, code, type="asset", parent=None, client=None, **kw):
        body = {"code": code, "name": f"Acct {code}", "type": type, **kw}
        if parent is not None:
            body["parent"] = parent
        return (client or self.client).post(URL, body, format="json")

    def test_create_child_and_listing_fields(self):
        p = self.mk("1900").data
        c = self.mk("1901", parent=p["id"])
        self.assertEqual(c.status_code, 201, c.content)
        self.assertEqual(c.data["parent"], p["id"])
        self.assertEqual(c.data["parent_name"], "Acct 1900")
        r = self.client.get(URL, {"parent": p["id"]})
        self.assertEqual([a["code"] for a in r.data], ["1901"])

    def test_type_mismatch_rejected(self):
        p = self.mk("1900").data
        r = self.mk("5900", type="expense", parent=p["id"])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "parent_type_mismatch")

    def test_other_org_parent_rejected(self):
        foreign = Account.objects.create(organization=self.other, code="1900", name="F", type="asset")
        r = self.mk("1901", parent=foreign.id)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "invalid_parent")

    def test_cycles_and_self_parent_rejected(self):
        a = self.mk("1900").data
        b = self.mk("1901", parent=a["id"]).data
        c = self.mk("1902", parent=b["id"]).data
        for target, parent in ((a, c), (a, a), (a, b)):
            r = self.client.patch(f"{URL}{target['id']}/", {"parent": parent["id"]}, format="json")
            self.assertEqual(r.status_code, 400, r.content)
            self.assertEqual(r.data["code"], "parent_cycle")

    def test_depth_limit(self):
        parent = None
        for i in range(hierarchy.MAX_DEPTH):
            r = self.mk(f"19{i:02d}", parent=parent)
            self.assertEqual(r.status_code, 201, r.content)
            parent = r.data["id"]
        r = self.mk("1999", parent=parent)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "parent_too_deep")

    def test_moving_a_branch_respects_subtree_depth(self):
        # chain of MAX_DEPTH-1, and a 2-level subtree elsewhere: attaching the subtree under the chain's leaf is too deep
        parent = None
        for i in range(hierarchy.MAX_DEPTH - 1):
            parent = self.mk(f"19{i:02d}", parent=parent).data["id"]
        top = self.mk("1950").data
        self.mk("1951", parent=top["id"])
        r = self.client.patch(f"{URL}{top['id']}/", {"parent": parent}, format="json")
        self.assertEqual(r.data["code"], "parent_too_deep")

    def test_type_change_blocked_when_children_exist(self):
        p = self.mk("1900").data
        self.mk("1901", parent=p["id"])
        r = self.client.patch(f"{URL}{p['id']}/", {"type": "liability"}, format="json")
        self.assertEqual(r.data["code"], "parent_type_mismatch")

    def test_cannot_deactivate_parent_with_active_children(self):
        p = self.mk("1900").data
        c = self.mk("1901", parent=p["id"]).data
        r = self.client.patch(f"{URL}{p['id']}/", {"is_active": False}, format="json")
        self.assertEqual(r.data["code"], "has_active_children")
        self.client.patch(f"{URL}{c['id']}/", {"is_active": False}, format="json")
        self.assertEqual(self.client.patch(f"{URL}{p['id']}/", {"is_active": False}, format="json").status_code, 200)
        r = self.mk("1902", parent=p["id"])  # inactive parent
        self.assertEqual(r.data["code"], "invalid_parent")

    def test_detach_to_top_level(self):
        p = self.mk("1900").data
        c = self.mk("1901", parent=p["id"]).data
        r = self.client.patch(f"{URL}{c['id']}/", {"parent": None}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data["parent"])

    def test_rollup_in_balances_and_parent_is_postable(self):
        cash_parent = self.mk("1850", name="Cash group").data
        sub = self.mk("1851", parent=cash_parent["id"], is_cash_bank=True).data
        # post to parent and child through the ledger service (the single writer)
        from apps.ledger.services import Line, post_journal
        sales = self.acct("SALES")
        post_journal(self.bakery, "2026-01-10", [Line(Account.objects.get(pk=sub["id"]), debit=D("60")), Line(sales, credit=D("60"))],
                     "test", 1, "T1", "", None, self.alice)
        post_journal(self.bakery, "2026-01-11", [Line(Account.objects.get(pk=cash_parent["id"]), debit=D("40")), Line(sales, credit=D("40"))],
                     "test", 2, "T2", "", None, self.alice)
        rows = {r["account"]: r for r in self.client.get(URL + "balances/").data}
        self.assertEqual(D(str(rows[sub["id"]]["debit"])), D("60"))
        self.assertEqual(D(str(rows[cash_parent["id"]]["debit"])), D("40"))
        self.assertEqual(D(str(rows[cash_parent["id"]]["rollup_debit"])), D("100"))
        self.assertEqual(D(str(rows[sub["id"]]["rollup_debit"])), D("60"))
        self.assertTrue(self.trial_balance_ok())

    def test_viewer_cannot_create_and_other_org_cannot_see(self):
        viewer = get_user_model().objects.create_user("vv", password="pw-viewer-12")
        Membership.objects.create(user=viewer, organization=self.bakery, role="viewer")
        r = self.mk("1990", client=self.as_user(viewer, self.bakery))
        self.assertEqual(r.status_code, 403)
        p = self.mk("1900").data
        r = self.as_user(self.bob, self.other).get(f"{URL}{p['id']}/")
        self.assertEqual(r.status_code, 404)


class SubAccountConcurrencyTests(TransactionTestCase):
    def test_concurrent_opposite_reparenting_cannot_form_cycle(self):
        if connection.vendor != "postgresql":
            self.skipTest("needs row-level locking")
        u = get_user_model().objects.create_user("zed", password="pw-zed-12345")
        org = create_organization(u, name="Cyc")
        a = Account.objects.create(organization=org, code="1900", name="A", type="asset")
        b = Account.objects.create(organization=org, code="1901", name="B", type="asset")
        results = []

        def go(x, y):
            try:
                hierarchy.update_account(org, x, parent=y)
                results.append("ok")
            except DomainError as e:
                results.append(e.code)
            finally:
                connection.close()

        ts = [threading.Thread(target=go, args=(a, b)), threading.Thread(target=go, args=(b, a))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["ok", "parent_cycle"])
