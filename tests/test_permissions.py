"""Granular module x action permissions: coverage of every route, presets, custom permissions and anti-escalation."""
from django.contrib.auth import get_user_model
from django.urls import get_resolver
from rest_framework.test import APIRequestFactory

from apps.core import perms
from apps.organizations.models import Membership

from .base import AccountingTestCase

User = get_user_model()
MEMBERS = "/api/v1/members/"
NOT_ORG_SCOPED = {"OrganizationViewSet", "LoginView", "LogoutView", "MeView", "InvitationLookupView", "InvitationAcceptView",
                  "PermissionCatalogView", "APIRootView", "RedirectView"}


def _view_classes():
    out = {}

    def walk(patterns):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns)
            else:
                cls = getattr(p.callback, "cls", None) or getattr(p.callback, "view_class", None)
                if cls is not None:
                    out[cls.__name__] = cls

    walk(get_resolver().url_patterns)
    return out


class RouteCoverageTests(AccountingTestCase):
    def test_every_org_scoped_view_and_action_is_mapped(self):
        classes = _view_classes()
        self.assertGreater(len(classes), 30)
        missing = sorted(n for n in classes if n not in perms.ROUTES and n not in NOT_ORG_SCOPED)
        self.assertEqual(missing, [], "views missing from apps.core.perms.ROUTES")
        rf = APIRequestFactory()
        m = Membership.objects.get(user=self.alice, organization=self.bakery)
        unmapped = []
        for name, cls in classes.items():
            if name not in perms.ROUTES or not hasattr(cls, "get_extra_actions"):
                continue
            actions = [("list", "GET"), ("retrieve", "GET"), ("create", "POST"), ("partial_update", "PATCH"), ("destroy", "DELETE")]
            for extra in cls.get_extra_actions():
                actions += [(extra.__name__, meth.upper()) for meth in extra.mapping]
            for act, meth in actions:
                handler = act if act not in ("list", "retrieve", "create", "partial_update", "destroy") else {
                    "list": "list", "retrieve": "retrieve", "create": "create", "partial_update": "partial_update", "destroy": "destroy"}[act]
                if not hasattr(cls, handler):
                    continue
                view = cls()
                view.action = act
                view.action_map = {}
                view.kwargs = {}
                req = getattr(rf, meth.lower())("/x/")
                from rest_framework.request import Request
                req = Request(req)
                req.organization, req.membership = self.bakery, m
                try:
                    perms.resolve(req, view)
                except perms.Unmapped:
                    unmapped.append(f"{name}.{act} {meth}")
        self.assertEqual(unmapped, [])


class PermissionTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.a = User.objects.create_user("userA", email="a@example.com", password="pw-userA-12345")
        self.b = User.objects.create_user("userB", email="b@example.com", password="pw-userB-12345")
        self.ma = Membership.objects.create(user=self.a, organization=self.bakery, role="accountant")
        self.mb = Membership.objects.create(user=self.b, organization=self.bakery, role="accountant")
        self.ca, self.cb = self.as_user(self.a, self.bakery), self.as_user(self.b, self.bakery)
        # the example from the specification
        self.set(self.ma, {"sales_invoices": ["view", "create"], "sales_orders": ["view"], "reports": ["view"]})
        self.set(self.mb, {"sales_invoices": ["view", "create", "edit"], "purchase_invoices": ["view", "create"],
                           "banking": ["view"], "reports": ["view", "export"]})

    def set(self, m, p):
        r = self.client.patch(f"{MEMBERS}{m.id}/", {"permissions": p}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r

    def sale(self, client, **extra):
        return client.post("/api/v1/sales/invoices/", {"party": self.customer.id, "date": "2026-01-10",
                           "items": [{"description": "x", "quantity": "1", "unit_price": "10"}], **extra}, format="json")

    def bill(self, client):
        return client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-10",
                           "items": [{"description": "x", "quantity": "1", "unit_price": "10"}]}, format="json")

    def test_user_a_from_spec(self):
        Membership.objects.filter(pk=self.ma.pk).update(role="admin")  # unrestricted branches; permissions stay custom
        self.assertEqual(self.ca.get("/api/v1/sales/invoices/?kind=invoice").status_code, 200)
        self.assertEqual(self.sale(self.ca).status_code, 201)
        self.assertEqual(self.ca.get("/api/v1/sales/orders/?kind=order").status_code, 200)
        self.assertEqual(self.ca.post("/api/v1/sales/orders/", {"kind": "order", "party": self.customer.id, "date": "2026-01-10",
                         "items": [{"description": "x", "quantity": "1", "unit_price": "1"}]}, format="json").status_code, 403)
        self.assertEqual(self.ca.get("/api/v1/sales/orders/?kind=quote").status_code, 403)  # quotes are a separate module
        for url in ("/api/v1/purchases/invoices/", "/api/v1/bank-statements/", "/api/v1/expenses/", "/api/v1/receipts/"):
            self.assertEqual(self.ca.get(url).status_code, 403, url)
        self.assertEqual(self.bill(self.ca).status_code, 403)
        self.assertEqual(self.ca.get("/api/v1/reports/profit-loss/").status_code, 200)
        inv = self.sale(self.client).data
        self.assertEqual(self.ca.post(f"/api/v1/sales/invoices/{inv['id']}/void/").status_code, 403)  # no void
        self.assertEqual(self.ca.post(f"/api/v1/sales/invoices/{inv['id']}/email/", {"to": "x@example.com"}, format="json").status_code, 403)
        # returns are a different module than invoices
        self.assertEqual(self.sale(self.ca, kind="return", original=inv["id"]).status_code, 403)
        self.assertEqual(self.ca.get("/api/v1/sales/invoices/?kind=return").status_code, 403)
        # credits / refunds belong to Customer credits
        self.assertEqual(self.ca.get("/api/v1/sales/invoices/credits/").status_code, 403)

    def test_user_b_from_spec(self):
        Membership.objects.filter(pk=self.mb.pk).update(role="admin")
        self.assertEqual(self.bill(self.cb).status_code, 201)
        self.assertEqual(self.cb.get("/api/v1/bank-statements/").status_code, 200)
        self.assertEqual(self.cb.post("/api/v1/bank-rules/", {"name": "r", "contains": "x", "category": self.acct("SALES").id}, format="json").status_code, 403)
        self.assertEqual(self.cb.get("/api/v1/expenses/").status_code, 403)

    def test_reference_data_stays_readable_but_writes_are_gated(self):
        for url in ("/api/v1/accounts/", "/api/v1/parties/", "/api/v1/tax-rates/", "/api/v1/branches/", "/api/v1/inventory/items/"):
            self.assertEqual(self.ca.get(url).status_code, 200, url)
        self.assertEqual(self.ca.post("/api/v1/accounts/", {"code": "9999", "name": "x", "type": "income"}, format="json").status_code, 403)
        self.assertEqual(self.ca.post("/api/v1/parties/", {"name": "x", "kind": "customer"}, format="json").status_code, 403)
        self.assertEqual(self.ca.get("/api/v1/accounts/balances/").status_code, 403)  # balances are chart-of-accounts data

    def test_reset_to_preset(self):
        r = self.client.patch(f"{MEMBERS}{self.ma.id}/", {"permissions": None}, format="json")
        self.assertFalse(r.data["custom_permissions"])
        self.assertEqual(self.ca.get("/api/v1/expenses/").status_code, 200)  # accountant preset again

    def test_owner_always_has_everything(self):
        Membership.objects.filter(user=self.alice, organization=self.bakery).update(permissions={"reports": ["view"]})
        self.assertEqual(self.client.get("/api/v1/bank-statements/").status_code, 200)
        self.assertEqual(self.sale(self.client).status_code, 201)
        me = next(o for o in self.client.get("/api/v1/organizations/").data["results"] if o["id"] == self.bakery.id)
        self.assertEqual(set(me["permissions"]["banking"]), set(perms.MODULE_ACTIONS["banking"]))

    def test_no_escalation(self):
        # an admin without banking permissions cannot hand banking (or owner) to anyone
        self.ma.role = "admin"
        self.ma.permissions = {**perms.as_lists(perms.PRESETS["admin"]), "banking": []}
        self.ma.save()
        r = self.ca.patch(f"{MEMBERS}{self.mb.id}/", {"permissions": {"banking": ["view", "edit"]}}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_escalation"))
        r = self.ca.patch(f"{MEMBERS}{self.mb.id}/", {"role": "admin", "permissions": None}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "permission_escalation"))  # admin preset includes banking
        r = self.ca.post("/api/v1/invitations/", {"email": "n@example.com", "role": "admin"}, format="json")
        self.assertEqual(r.status_code, 403)
        ok = self.ca.patch(f"{MEMBERS}{self.mb.id}/", {"permissions": {"reports": ["view"]}}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        # and nobody can edit their own permissions
        r = self.ca.patch(f"{MEMBERS}{self.ma.id}/", {"permissions": perms.as_lists(perms.PRESETS["admin"])}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "self_change"))

    def test_users_without_user_management_cannot_manage(self):
        self.assertEqual(self.ca.patch(f"{MEMBERS}{self.mb.id}/", {"permissions": None}, format="json").status_code, 403)
        self.assertEqual(self.ca.post("/api/v1/invitations/", {"email": "q@example.com", "role": "viewer"}, format="json").status_code, 403)
        self.assertEqual(self.ca.get(MEMBERS).status_code, 403)  # users.view removed by the custom set

    def test_organization_settings_need_manage(self):
        self.assertEqual(self.ca.patch(f"/api/v1/organizations/{self.bakery.id}/", {"approval_min_amount": "5"}, format="json").status_code, 403)
        self.assertEqual(self.ca.post(f"/api/v1/organizations/{self.bakery.id}/close-year/", {"year_end": "2025-12-31"}, format="json").status_code, 403)
        self.set(self.ma, {"settings": ["view", "manage"]})
        self.assertEqual(self.ca.patch(f"/api/v1/organizations/{self.bakery.id}/", {"approval_min_amount": "5"}, format="json").status_code, 200)

    def test_organization_settings_reads_require_view_without_blocking_org_selection_or_manage(self):
        url = f"/api/v1/organizations/{self.bakery.id}/"
        listing = self.ca.get("/api/v1/organizations/")
        self.assertEqual(listing.status_code, 200)
        org_row = next(row for row in listing.data["results"] if row["id"] == self.bakery.id)
        self.assertEqual(org_row["name"], self.bakery.name)
        for field in ("legal_name", "fiscal_year_start_month", "books_locked_through", "expense_approval_required",
                      "bill_approval_required", "approval_min_amount", "default_income_account", "effective_income_account"):
            self.assertNotIn(field, org_row)
        self.assertEqual(self.ca.get(url).status_code, 403)
        self.assertEqual(self.ca.get(f"{url}fiscal-years/").status_code, 403)

        other_org = self.as_user(self.bob, self.other)
        self.assertEqual(other_org.get(url).status_code, 404)
        self.assertEqual(other_org.get(f"{url}fiscal-years/").status_code, 404)

        self.set(self.ma, {"settings": ["view"]})
        detail = self.ca.get(url)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("approval_min_amount", detail.data)
        self.assertEqual(self.ca.get(f"{url}fiscal-years/").status_code, 200)

        self.set(self.ma, {"settings": ["manage"]})
        self.assertEqual(self.ca.get(url).status_code, 403)
        self.assertEqual(self.ca.patch(url, {"approval_min_amount": "5"}, format="json").status_code, 200)

        Membership.objects.filter(user=self.alice, organization=self.bakery).update(permissions={"reports": ["view"]})
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(f"{url}fiscal-years/").status_code, 200)

    def test_viewer_preset_matches_previous_behaviour(self):
        from apps.organizations.models import Branch, BranchAccess

        v = User.objects.create_user("vee", password="pw-vee-1234567")
        mv = Membership.objects.create(user=v, organization=self.bakery, role="viewer")
        br = Branch.objects.create(organization=self.bakery, name="Main")
        BranchAccess.objects.create(membership=mv, branch=br)  # viewers see only their branches (unchanged behaviour)
        cv = self.as_user(v, self.bakery)
        inv = self.sale(self.client, branch=br.id).data
        self.assertEqual(cv.get(f"/api/v1/sales/invoices/{inv['id']}/pdf/").status_code, 200)  # viewing PDFs is fine
        self.assertEqual(cv.post(f"/api/v1/sales/invoices/{inv['id']}/email/", {"to": "x@example.com"}, format="json").status_code, 403)
        self.assertEqual(self.sale(cv).status_code, 403)
        self.assertEqual(cv.get("/api/v1/bank-statements/").status_code, 403)  # banking: unrestricted roles only (unchanged)

    def test_catalog_and_me(self):
        cat = self.client.get("/api/v1/permissions/catalog/").data
        self.assertEqual(cat["actions"], perms.ACTIONS)
        self.assertIn("sales_invoices", {m["key"] for m in cat["modules"]})
        self.assertEqual(set(cat["presets"]), {"owner", "admin", "accountant", "viewer"})
        org = next(o for o in self.ca.get("/api/v1/organizations/").data["results"] if o["id"] == self.bakery.id)
        self.assertEqual(org["permissions"]["sales_invoices"], ["view", "create"])
        self.assertEqual(org["permissions"]["banking"], [])
