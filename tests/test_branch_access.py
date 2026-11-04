"""Branch-level access: owners/admins reach every branch; other members only their assigned branches."""
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.ledger.models import JournalLine, JournalTransaction
from apps.organizations.models import Branch, BranchAccess, Membership
from apps.organizations.services import create_organization

from .base import AccountingTestCase, D

User = get_user_model()
DATE = "2026-03-10"
REPORTS = ("trial-balance", "profit-loss", "balance-sheet", "aging", "vendor", "sales-summary", "vat-summary")


class BranchBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.north = Branch.objects.create(organization=self.bakery, name="North")
        self.south = Branch.objects.create(organization=self.bakery, name="South")
        self.closed = Branch.objects.create(organization=self.bakery, name="Closed", is_active=False)
        self.foreign = Branch.objects.create(organization=self.other, name="Foreign")  # another organization's branch
        self.nora = self.member("nora", Membership.Role.ACCOUNTANT, [self.north])   # assigned North
        self.vic = self.member("vic", Membership.Role.VIEWER, [self.north])         # viewer, assigned North
        self.sam = self.member("sam", Membership.Role.ACCOUNTANT, [])               # accountant, NO assignments
        self.dan = self.member("dan", Membership.Role.ADMIN, [])                    # admin, no rows needed
        self.c_nora, self.c_vic, self.c_sam, self.c_dan = (self.as_user(u, self.bakery) for u in (self.nora, self.vic, self.sam, self.dan))

    def member(self, name, role, branches):
        u = User.objects.create_user(name, password="pw-12345-xx")
        m = Membership.objects.create(user=u, organization=self.bakery, role=role)
        for b in branches:
            BranchAccess.objects.create(membership=m, branch=b)
        return u

    def membership(self, user, org=None):
        return Membership.objects.get(user=user, organization=org or self.bakery)

    # ---- document bodies
    def body(self, kind, branch, amount="100"):
        b = {"branch": branch.id if branch else None}
        if kind == "sales":
            return {**b, "party": self.customer.id, "date": DATE, "items": [{"description": "x", "quantity": "1", "unit_price": amount, "tax_rate": "10"}]}
        if kind == "purchases":
            return {**b, "party": self.supplier.id, "date": DATE, "items": [{"description": "x", "quantity": "1", "unit_price": amount, "tax_rate": "10"}]}
        if kind == "receipts":
            return {**b, "party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": amount, "date": DATE}
        if kind == "payments":
            return {**b, "party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": amount, "date": DATE}
        lines = [{"account": self.acct("PURCHASES").id, "debit": amount}, {"account": self.acct("EQUITY").id, "credit": amount}]
        return {**b, "type": "general", "date": DATE, "lines": lines}

    URLS = {"sales": "/api/v1/sales/invoices/", "purchases": "/api/v1/purchases/invoices/", "receipts": "/api/v1/receipts/",
            "payments": "/api/v1/payments/", "vouchers": "/api/v1/vouchers/"}

    def post(self, client, kind, branch, amount="100"):
        return client.post(self.URLS[kind], self.body(kind, branch, amount), format="json")

    def state(self):
        return JournalTransaction.objects.count(), JournalLine.objects.count()

    def seed(self):
        """Owner posts one document of every kind in North, South and (sales/purchases) with no branch."""
        ids = {}
        for kind in self.URLS:
            for tag, br, amt in (("n", self.north, "100"), ("s", self.south, "200")):
                r = self.post(self.client, kind, br, amt)
                self.assertEqual(r.status_code, 201, f"{kind} {tag}: {r.data}")
                ids[(kind, tag)] = r.data["id"]
        for kind in ("sales", "purchases"):
            ids[(kind, "x")] = self.post(self.client, kind, None, "300").data["id"]
        return ids

    def get_json(self, client, url, **params):
        r = client.get(url, params)
        return r.status_code, (json.loads(r.content) if r.content else None)


class AssignmentApiTests(BranchBase):
    def test_admin_can_assign_and_revoke_and_effect_is_immediate(self):
        r = self.c_dan.post("/api/v1/branch-access/", {"membership": self.membership(self.sam).id, "branch": self.south.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self.post(self.c_sam, "sales", self.south).status_code, 201)
        self.assertEqual(self.c_dan.delete(f"/api/v1/branch-access/{r.data['id']}/").status_code, 204)
        self.assertEqual(self.post(self.c_sam, "sales", self.south).data["code"], "branch_forbidden")

    def test_restricted_members_cannot_manage_assignments(self):
        body = {"membership": self.membership(self.nora).id, "branch": self.south.id}
        for c in (self.c_nora, self.c_vic, self.c_sam):
            self.assertEqual(c.post("/api/v1/branch-access/", body, format="json").status_code, 403)
        row = BranchAccess.objects.get(membership=self.membership(self.nora), branch=self.north)
        self.assertEqual(self.c_nora.delete(f"/api/v1/branch-access/{row.id}/").status_code, 403)
        self.assertTrue(BranchAccess.objects.filter(pk=row.pk).exists())

    def test_cross_organization_assignments_are_rejected(self):
        m_sam = self.membership(self.sam).id
        r = self.client.post("/api/v1/branch-access/", {"membership": m_sam, "branch": self.foreign.id}, format="json")
        self.assertEqual(r.status_code, 400)
        bob_membership = self.membership(self.bob, self.other).id
        r = self.client.post("/api/v1/branch-access/", {"membership": bob_membership, "branch": self.south.id}, format="json")
        self.assertEqual(r.status_code, 400)
        # the other organization's owner cannot grant access to our branch either
        r = self.as_user(self.bob, self.other).post("/api/v1/branch-access/", {"membership": bob_membership, "branch": self.south.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(BranchAccess.objects.filter(branch__in=[self.foreign]).exists())

    def test_model_refuses_a_cross_organization_row(self):
        with self.assertRaises(ValidationError):
            BranchAccess.objects.create(membership=self.membership(self.sam), branch=self.foreign)

    def test_duplicate_assignment_is_rejected(self):
        r = self.client.post("/api/v1/branch-access/", {"membership": self.membership(self.nora).id, "branch": self.north.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_listing_assignments(self):
        self.assertEqual(self.client.get("/api/v1/branch-access/").data["count"], 2)       # owner sees all rows
        mine = self.c_nora.get("/api/v1/branch-access/").data
        self.assertEqual([x["username"] for x in mine["results"]], ["nora"])                 # restricted sees only their own
        self.assertEqual(self.c_sam.get("/api/v1/branch-access/").data["count"], 0)
        self.assertEqual(self.as_user(self.bob, self.other).get("/api/v1/branch-access/").data["count"], 0)

    def test_a_bad_row_pointing_at_a_foreign_branch_grants_nothing(self):
        """Even if bad data bypasses validation, reads join through the branch's own organization."""
        BranchAccess.objects.bulk_create([BranchAccess(membership=self.membership(self.sam), branch=self.foreign)])
        self.assertEqual(self.c_sam.get("/api/v1/branches/").data["count"], 0)
        self.assertEqual(self.post(self.c_sam, "sales", self.foreign).status_code, 400)


class BranchListTests(BranchBase):
    def names(self, client):
        return sorted(b["name"] for b in client.get("/api/v1/branches/").data["results"])

    def test_restricted_users_only_see_their_branches(self):
        self.assertEqual(self.names(self.c_nora), ["North"])
        self.assertEqual(self.names(self.c_vic), ["North"])
        self.assertEqual(self.names(self.c_sam), [])

    def test_owner_and_admin_see_every_branch_of_their_organization_only(self):
        for c in (self.client, self.c_dan):
            self.assertEqual(self.names(c), ["Closed", "North", "South"])  # never "Foreign"

    def test_unassigned_branch_detail_is_404_and_branches_stay_admin_writable(self):
        self.assertEqual(self.c_nora.get(f"/api/v1/branches/{self.south.id}/").status_code, 404)
        self.assertEqual(self.c_nora.get(f"/api/v1/branches/{self.foreign.id}/").status_code, 404)
        self.assertEqual(self.c_nora.patch(f"/api/v1/branches/{self.north.id}/", {"name": "Hacked"}, format="json").status_code, 403)
        self.assertEqual(self.c_nora.post("/api/v1/branches/", {"name": "New"}, format="json").status_code, 403)


class PostingTests(BranchBase):
    def test_assigned_branch_is_allowed_for_every_document_type(self):
        for kind in self.URLS:
            r = self.post(self.c_nora, kind, self.north)
            self.assertEqual(r.status_code, 201, f"{kind}: {r.data}")
            self.assertEqual(r.data["branch"], self.north.id)
        self.assertTrue(self.trial_balance_ok())

    def test_unassigned_branch_in_same_organization_is_refused_for_every_type(self):
        for kind in self.URLS:
            before = self.state()
            r = self.post(self.c_nora, kind, self.south)
            self.assertEqual((r.status_code, r.data["code"]), (400, "branch_forbidden"), kind)
            self.assertEqual(self.state(), before, f"{kind} left residue")

    def test_branch_is_required_for_restricted_users(self):
        for kind in self.URLS:
            before = self.state()
            r = self.post(self.c_nora, kind, None)
            self.assertEqual((r.status_code, r.data["code"]), (400, "branch_required"), kind)
            self.assertEqual(self.state(), before)

    def test_cross_organization_branch_is_refused_for_everyone(self):
        for client in (self.c_nora, self.c_dan, self.client):
            for kind in self.URLS:
                before = self.state()
                r = self.post(client, kind, self.foreign)
                self.assertEqual(r.status_code, 400, f"{kind}: {r.data}")
                self.assertEqual(self.state(), before)

    def test_user_with_no_assignments_cannot_post_anywhere(self):
        for br in (None, self.north, self.south, self.foreign):
            for kind in self.URLS:
                before = self.state()
                r = self.post(self.c_sam, kind, br)
                self.assertEqual(r.status_code, 400, f"{kind} {br}")
                self.assertEqual(self.state(), before)

    def test_owner_and_admin_may_post_to_any_branch_or_none(self):
        for client in (self.client, self.c_dan):
            for br in (self.north, self.south, None):
                for kind in self.URLS:
                    r = self.post(client, kind, br)
                    self.assertEqual(r.status_code, 201, f"{kind} {br}: {r.data}")

    def test_inactive_branch_is_refused_even_for_admins(self):
        for client in (self.client, self.c_dan):
            self.assertEqual(self.post(client, "sales", self.closed).data["code"], "inactive_branch")

    def test_viewer_still_cannot_post_even_in_an_assigned_branch(self):
        self.assertEqual(self.post(self.c_vic, "sales", self.north).status_code, 403)

    def test_revoking_access_blocks_posting_at_once(self):
        self.assertEqual(self.post(self.c_nora, "sales", self.north).status_code, 201)
        BranchAccess.objects.filter(membership=self.membership(self.nora)).delete()
        self.assertEqual(self.post(self.c_nora, "sales", self.north).data["code"], "branch_forbidden")

    def test_role_changes_take_effect(self):
        m = self.membership(self.nora)
        m.role = Membership.Role.ADMIN
        m.save()
        self.assertEqual(self.post(self.as_user(self.nora, self.bakery), "sales", self.south).status_code, 201)
        m.role = Membership.Role.ACCOUNTANT
        m.save()
        BranchAccess.objects.filter(membership=m).delete()
        self.assertEqual(self.post(self.as_user(self.nora, self.bakery), "sales", self.south).status_code, 400)


class VisibilityTests(BranchBase):
    REGISTERS = {"sales": "/api/v1/sales/invoices/", "purchases": "/api/v1/purchases/invoices/", "receipts": "/api/v1/receipts/",
                 "payments": "/api/v1/payments/", "vouchers": "/api/v1/vouchers/"}

    def setUp(self):
        super().setUp()
        self.ids = self.seed()

    def test_restricted_registers_show_only_assigned_branch_documents(self):
        for kind, url in self.REGISTERS.items():
            rows = self.c_nora.get(url).data["results"]
            self.assertEqual([r["id"] for r in rows], [self.ids[(kind, "n")]], kind)
        self.assertEqual({t["source_type"].split(".")[0] for t in self.c_nora.get("/api/v1/journal/").data["results"]}, {"sales", "purchases", "treasury", "voucher"})
        self.assertEqual(self.c_nora.get("/api/v1/journal/").data["count"], 5)  # one north entry per document type

    def test_owner_and_admin_see_everything_including_unbranched(self):
        for c in (self.client, self.c_dan):
            self.assertEqual(c.get(self.REGISTERS["sales"]).data["count"], 3)
            self.assertEqual(c.get(self.REGISTERS["purchases"]).data["count"], 3)
            self.assertEqual(c.get(self.REGISTERS["receipts"]).data["count"], 2)
            self.assertEqual(c.get("/api/v1/journal/").data["count"], 12)

    def test_user_with_no_assignments_sees_nothing(self):
        for url in (*self.REGISTERS.values(), "/api/v1/journal/", "/api/v1/party-balances/"):
            r = self.c_sam.get(url)
            self.assertEqual(r.status_code, 200)
            data = r.data
            self.assertEqual(data["count"] if isinstance(data, dict) else len(data), 0, url)

    def test_documents_of_other_branches_are_404_not_403_and_cannot_be_voided(self):
        for kind, url in self.REGISTERS.items():
            for tag in ("s", "x"):
                pk = self.ids.get((kind, tag))
                if pk is None:
                    continue
                self.assertEqual(self.c_nora.get(f"{url}{pk}/").status_code, 404, f"{kind} {tag}")
                self.assertEqual(self.c_nora.post(f"{url}{pk}/void/").status_code, 404, f"{kind} {tag} void")
                self.assertEqual(self.client.get(f"{url}{pk}/").data["status"], "posted")
        txn = JournalTransaction.objects.filter(branch=self.south).first()
        self.assertEqual(self.c_nora.get(f"/api/v1/journal/{txn.id}/").status_code, 404)

    def test_restricted_user_can_void_in_assigned_branch_but_viewer_cannot(self):
        url = f"/api/v1/sales/invoices/{self.ids[('sales', 'n')]}/void/"
        self.assertEqual(self.c_vic.post(url).status_code, 403)
        self.assertEqual(self.c_nora.post(url).status_code, 200)

    def test_filters_and_search_cannot_widen_the_scope(self):
        sx = self.ids[("sales", "s")]
        for params in ({"search": ""}, {"party": self.customer.id}, {"page_size": 200}, {"ordering": "-total"}, {"status": "posted"}, {"branch": self.south.id}):
            ids = [r["id"] for r in self.c_nora.get("/api/v1/sales/invoices/", params).data["results"]]
            self.assertNotIn(sx, ids, params)
            self.assertNotIn(self.ids[("sales", "x")], ids, params)

    def test_viewer_is_scoped_too(self):
        self.assertEqual(self.c_vic.get("/api/v1/sales/invoices/").data["count"], 1)

    def test_balances_are_scoped(self):
        pb = {r["party"]: r for r in self.c_nora.get("/api/v1/party-balances/").data}
        self.assertEqual(set(pb), {self.customer.id, self.supplier.id})
        # North only: sale 110 invoiced - 100 received; purchase 110 invoiced - 100 paid
        self.assertEqual(D(str(pb[self.customer.id]["receivable"])), D("10.00"))
        admin_pb = {r["party"]: r for r in self.c_dan.get("/api/v1/party-balances/").data}
        self.assertEqual(D(str(admin_pb[self.customer.id]["receivable"])), D("10.00") + D("220.00") - D("200") + D("330.00"))
        ab = {r["code"]: r for r in self.c_nora.get("/api/v1/accounts/balances/").data}
        self.assertEqual(D(str(ab["1200"]["debit"])), D("110.00"))  # North's AR debits only


class AllocationBranchTests(BranchBase):
    def setUp(self):
        super().setUp()
        self.inv_n = self.post(self.client, "sales", self.north, "100").data["id"]
        self.inv_s = self.post(self.client, "sales", self.south, "100").data["id"]

    def receipt(self, client, branch, allocs, amount="50"):
        body = {**self.body("receipts", branch, amount), "allocations": [{"invoice": i, "amount": a} for i, a in allocs]}
        return client.post("/api/v1/receipts/", body, format="json")

    def test_same_branch_allocation_works(self):
        self.assertEqual(self.receipt(self.c_nora, self.north, [(self.inv_n, "50")]).status_code, 201)

    def test_cross_branch_allocation_is_refused_for_admins_too(self):
        r = self.receipt(self.client, self.north, [(self.inv_s, "50")])
        self.assertEqual((r.status_code, r.data["code"]), (400, "branch_mismatch"))

    def test_restricted_user_cannot_reference_an_invoice_in_an_unassigned_branch(self):
        before = self.state()
        r = self.receipt(self.c_nora, self.north, [(self.inv_s, "50")])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.state(), before)
        rid = self.post(self.c_nora, "receipts", self.north).data["id"]
        r = self.c_nora.post(f"/api/v1/receipts/{rid}/allocate/", {"allocations": [{"invoice": self.inv_s, "amount": "10"}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_open_invoices_list_is_scoped(self):
        ids = [i["id"] for i in self.c_nora.get("/api/v1/receipts/open-invoices/", {"party": self.customer.id}).data]
        self.assertEqual(ids, [self.inv_n])
        self.assertEqual(self.c_sam.get("/api/v1/receipts/open-invoices/", {"party": self.customer.id}).data, [])
        both = [i["id"] for i in self.c_dan.get("/api/v1/receipts/open-invoices/", {"party": self.customer.id}).data]
        self.assertEqual(sorted(both), sorted([self.inv_n, self.inv_s]))

    def test_returns_must_stay_in_the_originals_branch_and_be_reachable(self):
        ret = lambda client, br, orig: client.post("/api/v1/sales/invoices/", {**self.body("sales", br, "10"), "kind": "return", "original": orig}, format="json")
        self.assertEqual(ret(self.c_nora, self.north, self.inv_n).status_code, 201)
        r = ret(self.client, self.south, self.inv_n)
        self.assertEqual((r.status_code, r.data["code"]), (400, "branch_mismatch"))
        r = ret(self.c_nora, self.north, self.inv_s)  # original lives in a branch nora cannot reach
        self.assertEqual(r.status_code, 400)


class ReportBranchTests(BranchBase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_restricted_report_equals_the_admin_report_filtered_to_that_branch(self):
        for rep in REPORTS:
            s1, mine = self.get_json(self.c_nora, f"/api/v1/reports/{rep}/")
            s2, admin_n = self.get_json(self.c_dan, f"/api/v1/reports/{rep}/", branch=self.north.id)
            self.assertEqual((s1, s2), (200, 200), rep)
            self.assertEqual(mine, admin_n, f"{rep}: restricted view must equal the North-only view")
            _, admin_all = self.get_json(self.c_dan, f"/api/v1/reports/{rep}/")
            self.assertNotEqual(mine, admin_all, f"{rep}: restricted view must not equal the all-branches view")

    def test_specific_figures(self):
        _, pl = self.get_json(self.c_nora, "/api/v1/reports/profit-loss/")
        _, pl_all = self.get_json(self.c_dan, "/api/v1/reports/profit-loss/")
        _, pl_s = self.get_json(self.c_dan, "/api/v1/reports/profit-loss/", branch=self.south.id)
        self.assertEqual(pl["total_income"], 100.0)       # North only
        self.assertEqual(pl_s["total_income"], 200.0)     # South only (admin filter)
        self.assertEqual(pl_all["total_income"], 600.0)   # 100 + 200 + 300 unbranched
        _, vat = self.get_json(self.c_nora, "/api/v1/reports/vat-summary/")
        self.assertEqual(D(str(vat["output_tax"]["net"])), D("10.00"))
        _, vat_all = self.get_json(self.c_dan, "/api/v1/reports/vat-summary/")
        self.assertEqual(D(str(vat_all["output_tax"]["net"])), D("60.00"))

    def test_restricted_trial_balance_balances(self):
        _, tb = self.get_json(self.c_nora, "/api/v1/reports/trial-balance/")
        self.assertTrue(tb["balanced"])
        self.assertGreater(tb["total_debit"], 0)
        self.assertEqual(tb["total_debit"], tb["total_credit"])

    def test_requesting_a_branch_outside_the_scope_is_forbidden_not_ignored(self):
        for rep in REPORTS:
            for bid in (self.south.id, self.foreign.id):
                r = self.c_nora.get(f"/api/v1/reports/{rep}/", {"branch": bid})
                self.assertEqual(r.status_code, 403, f"{rep} branch={bid}")
        r = self.c_nora.get("/api/v1/reports/profit-loss/", {"branch": self.north.id})
        self.assertEqual(r.status_code, 200)

    def test_user_without_assignments_gets_empty_reports_not_everything(self):
        _, pl = self.get_json(self.c_sam, "/api/v1/reports/profit-loss/")
        self.assertEqual((pl["total_income"], pl["total_expenses"], pl["net_profit"]), (0, 0, 0))
        _, tb = self.get_json(self.c_sam, "/api/v1/reports/trial-balance/")
        self.assertEqual((tb["rows"], tb["total_debit"]), ([], 0))
        for rep in ("aging", "vendor", "sales-summary"):
            _, data = self.get_json(self.c_sam, f"/api/v1/reports/{rep}/")
            self.assertEqual(data["rows"], [], rep)
        _, vat = self.get_json(self.c_sam, "/api/v1/reports/vat-summary/")
        self.assertEqual(D(str(vat["net_payable"])), D("0"))
        self.assertEqual(self.c_sam.get("/api/v1/reports/profit-loss/", {"branch": self.north.id}).status_code, 403)

    def test_admin_branch_filter_is_validated_against_the_organization(self):
        for rep in ("trial-balance", "aging", "sales-summary"):
            self.assertEqual(self.c_dan.get(f"/api/v1/reports/{rep}/", {"branch": self.foreign.id}).status_code, 404, rep)
            self.assertEqual(self.c_dan.get(f"/api/v1/reports/{rep}/", {"branch": "abc"}).status_code, 400, rep)

    def test_aging_and_sales_summary_rows_are_branch_only_for_restricted_users(self):
        _, ag = self.get_json(self.c_nora, "/api/v1/reports/aging/", as_of="2026-12-31")
        self.assertEqual(D(str(ag["totals"]["ledger_balance"])), D("10.00"))  # North: 110 invoiced - 100 received
        _, ss = self.get_json(self.c_nora, "/api/v1/reports/sales-summary/", group_by="customer")
        self.assertEqual(D(str(ss["totals"]["sales"])), D("100.00"))


class OrganizationIsolationTests(BranchBase):
    def test_access_in_one_organization_gives_nothing_in_another(self):
        # nora is also an ADMIN of her own second organization; that must not widen her scope in the bakery
        own = create_organization(self.nora, name="Nora Co")
        Branch.objects.create(organization=own, name="Own Branch")
        in_own = self.as_user(self.nora, own)
        self.assertEqual(in_own.get("/api/v1/branches/").data["count"], 1)
        self.assertEqual(self.c_nora.get("/api/v1/branches/").data["count"], 1)  # still only North in the bakery
        self.assertEqual(self.post(self.c_nora, "sales", self.south).data["code"], "branch_forbidden")

    def test_header_for_an_organization_you_do_not_belong_to_is_refused(self):
        c = self.as_user(self.nora, self.other)
        self.assertEqual(c.get("/api/v1/sales/invoices/").status_code, 403)
        self.assertEqual(c.get("/api/v1/reports/profit-loss/").status_code, 403)

    def test_other_organizations_documents_never_appear(self):
        theirs = self.as_user(self.bob, self.other)
        theirs.post("/api/v1/vouchers/", {"type": "journal", "date": DATE, "lines": [
            {"account": self.acct("CASH", self.other).id, "debit": "5"}, {"account": self.acct("EQUITY", self.other).id, "credit": "5"}],
            "branch": self.foreign.id}, format="json")
        self.assertEqual(self.c_dan.get("/api/v1/vouchers/").data["count"], 0)
        self.assertEqual(self.c_nora.get("/api/v1/journal/").data["count"], 0)
