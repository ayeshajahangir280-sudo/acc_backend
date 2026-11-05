"""Role matrix for the controls added in this phase. Owner/admin/accountant/viewer stay as they were; no custom permissions exist,
so this pins what each role may do and proves the server (not the UI) enforces it."""
from django.contrib.auth import get_user_model

from apps.organizations.models import Membership

from .test_expenses import ExpenseBase, URL
from .test_approvals import require

User = get_user_model()


class RoleMatrixTests(ExpenseBase):
    def test_matrix(self):
        require(self.bakery, bill=False)
        org = f"/api/v1/organizations/{self.bakery.id}/"
        e = self.make(client=self.c_dan).data                      # a pending expense created by the admin
        owner, admin, acct, viewer = self.client, self.c_dan, self.c_sam, self.c_vic
        # status codes of: approve, reject (reason sent), lock-history, reopen-year, approval settings, bank rules, create expense
        def codes(c):
            return {
                "approve": c.post(f"{URL}{e['id']}/approve/", {}, format="json").status_code,
                "lock_history": c.get(org + "lock-history/").status_code,
                "reopen": c.post(org + "reopen-year/", {"year_end": "2020-12-31", "reason": "x y z"}, format="json").status_code,
                "settings": c.patch(org, {"approval_min_amount": "5"}, format="json").status_code,
                "bank_rules": c.get("/api/v1/bank-rules/").status_code,
                "expense": c.post(URL, self.body(branch=self.north, amount="1"), format="json").status_code if c is not acct else None,
            }
        self.assertEqual(codes(viewer), {"approve": 403, "lock_history": 403, "reopen": 403, "settings": 403, "bank_rules": 403, "expense": 403})
        a = codes(acct)
        self.assertEqual((a["lock_history"], a["reopen"], a["settings"], a["bank_rules"]), (403, 403, 403, 403))
        self.assertEqual(a["approve"], 404)                         # sam has no branch access, so the unbranched expense is invisible
        d = codes(admin)
        self.assertEqual((d["lock_history"], d["reopen"], d["settings"], d["bank_rules"]), (200, 403, 200, 200))
        self.assertEqual(d["approve"], 400)                         # admin cannot approve their own expense
        o = codes(owner)
        self.assertEqual((o["lock_history"], o["settings"], o["bank_rules"], o["approve"]), (200, 200, 200, 200))
        self.assertEqual(o["reopen"], 400)                          # owner may call it; nothing to reopen
