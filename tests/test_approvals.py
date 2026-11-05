"""Approval workflow for expenses and purchase bills: nothing posts until approved; controls run at approval time."""
import datetime

from apps.inventory.models import StockMovement
from apps.ledger.models import JournalTransaction
from apps.organizations.models import Organization

from .base import D
from .test_costing import CostBase
from .test_expenses import ExpenseBase, URL

BILLS = "/api/v1/purchases/invoices/"


def require(org, expense=True, bill=True, minimum="0"):
    Organization.objects.filter(pk=org.pk).update(expense_approval_required=expense, bill_approval_required=bill, approval_min_amount=D(minimum))


class ExpenseApprovalTests(ExpenseBase):
    def pend(self, **kw):
        require(self.bakery, bill=False)
        r = self.make(client=self.c_nora, branch=self.north, **kw)
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def test_off_by_default_posts_immediately(self):
        r = self.make()
        self.assertEqual(r.data["status"], "posted")
        self.assertEqual(self.balance("CASH"), D("-110.00"))

    def test_pending_expense_has_no_journal_and_no_balance_effect(self):
        e = self.pend()
        self.assertEqual(e["status"], "pending")
        self.assertEqual(JournalTransaction.objects.filter(source_type="expenses.expense").count(), 0)
        self.assertEqual(self.balance("CASH"), D("0"))
        self.assertEqual(self.balance("TAX_INPUT"), D("0"))

    def test_threshold_only_holds_large_documents(self):
        require(self.bakery, bill=False, minimum="500")
        self.assertEqual(self.make(amount="100").data["status"], "posted")
        self.assertEqual(self.make(amount="1000").data["status"], "pending")

    def test_accountant_cannot_approve_or_reject(self):
        e = self.pend()
        for action in ("approve", "reject"):
            r = self.c_nora.post(f"{URL}{e['id']}/{action}/", {"reason": "no"}, format="json")
            self.assertEqual(r.data["code"], "approval_forbidden")
        self.assertEqual(self.balance("CASH"), D("0"))

    def test_viewer_gets_403(self):
        e = self.pend()
        self.assertEqual(self.c_vic.post(f"{URL}{e['id']}/approve/", {}, format="json").status_code, 403)

    def test_admin_approves_and_it_posts_with_branch(self):
        e = self.pend()
        r = self.c_dan.post(f"{URL}{e['id']}/approve/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["status"], "posted")
        self.assertEqual(self.balance("CASH"), D("-110.00"))
        j = JournalTransaction.objects.get(source_type="expenses.expense")
        self.assertEqual(j.branch_id, self.north.id)
        self.assertTrue(self.trial_balance_ok())
        # approving twice does nothing more
        again = self.c_dan.post(f"{URL}{e['id']}/approve/", {}, format="json")
        self.assertEqual(again.data["code"], "not_pending")
        self.assertEqual(JournalTransaction.objects.filter(source_type="expenses.expense").count(), 1)
        # the posted expense can now be voided like any other
        self.assertEqual(self.c_dan.post(f"{URL}{e['id']}/void/", {}, format="json").status_code, 200)
        self.assertEqual(self.balance("CASH"), D("0"))

    def test_admin_cannot_approve_own_but_owner_can(self):
        require(self.bakery, bill=False)
        mine = self.make(client=self.c_dan).data
        self.assertEqual(self.c_dan.post(f"{URL}{mine['id']}/approve/", {}, format="json").data["code"], "self_approval")
        self.assertEqual(self.client.post(f"{URL}{mine['id']}/approve/", {}, format="json").status_code, 200)  # alice is the owner
        own = self.make().data
        self.assertEqual(self.client.post(f"{URL}{own['id']}/approve/", {}, format="json").status_code, 200)

    def test_period_lock_is_enforced_at_creation_and_at_approval(self):
        require(self.bakery, bill=False)
        e = self.make(client=self.c_nora, branch=self.north, date="2026-03-10").data
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through=datetime.date(2026, 3, 31))
        r = self.c_dan.post(f"{URL}{e['id']}/approve/", {}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(self.client.get(f"{URL}{e['id']}/").data["status"], "pending")   # still pending, nothing posted
        self.assertEqual(JournalTransaction.objects.filter(source_type="expenses.expense").count(), 0)
        late = self.make(client=self.c_nora, branch=self.north, date="2026-03-20")
        self.assertEqual(late.status_code, 400)                                            # refused up front, not parked

    def test_branch_rules_apply_when_submitting(self):
        require(self.bakery, bill=False)
        r = self.make(client=self.c_nora, branch=self.south)                              # nora only has North
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.data["code"], "branch_forbidden")

    def test_reject_needs_reason_and_is_final(self):
        e = self.pend()
        self.assertEqual(self.c_dan.post(f"{URL}{e['id']}/reject/", {}, format="json").data["code"], "reason_required")
        r = self.c_dan.post(f"{URL}{e['id']}/reject/", {"reason": "Not a business cost"}, format="json")
        self.assertEqual((r.data["status"], r.data["rejection_reason"]), ("rejected", "Not a business cost"))
        self.assertEqual(self.c_dan.post(f"{URL}{e['id']}/approve/", {}, format="json").data["code"], "not_pending")
        self.assertEqual(self.c_dan.post(f"{URL}{e['id']}/void/", {}, format="json").data["code"], "not_posted")
        self.assertEqual(self.balance("CASH"), D("0"))

    def test_pending_cannot_be_voided_and_other_org_cannot_see_it(self):
        e = self.pend()
        self.assertEqual(self.c_dan.post(f"{URL}{e['id']}/void/", {}, format="json").data["code"], "not_posted")
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.post(f"{URL}{e['id']}/approve/", {}, format="json").status_code, 404)

    def test_only_admins_can_change_the_setting(self):
        r = self.c_nora.patch(f"/api/v1/organizations/{self.bakery.id}/", {"expense_approval_required": True}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client.patch(f"/api/v1/organizations/{self.bakery.id}/", {"expense_approval_required": True, "approval_min_amount": "250"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.data["expense_approval_required"])


class BillApprovalTests(ExpenseBase):
    def bill(self, client=None, **kw):
        body = {"party": self.supplier.id, "date": "2026-03-10", "items": [{"description": "Flour", "quantity": "2", "unit_price": "50", "tax_rate": "10"}], **kw}
        return (client or self.c_nora).post(BILLS, body, format="json")

    def test_pending_bill_does_not_hit_payables_or_accept_payments(self):
        require(self.bakery, expense=False)
        r = self.bill(branch=self.north.id)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("0"))
        pay = self.client.post("/api/v1/payments/", {"party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": "110", "date": "2026-03-11",
                                                      "allocations": [{"invoice": r.data["id"], "amount": "110"}]}, format="json")
        self.assertGreaterEqual(pay.status_code, 400, pay.content)
        self.assertEqual(self.balance("CASH"), D("0"))
        # no PDF email for an unapproved bill
        self.assertEqual(self.client.post(f"{BILLS}{r.data['id']}/email/", {"to": "a@b.co"}, format="json").data["code"], "not_approved")

    def test_approve_posts_and_reject_leaves_nothing(self):
        require(self.bakery, expense=False)
        a = self.bill(branch=self.north.id).data
        b = self.bill(branch=self.north.id).data
        r = self.c_dan.post(f"{BILLS}{a['id']}/approve/", {}, format="json")
        self.assertEqual((r.status_code, r.data["status"]), (200, "posted"), r.content)
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("-110.00"))
        r = self.c_dan.post(f"{BILLS}{b['id']}/reject/", {"reason": "Duplicate"}, format="json")
        self.assertEqual(r.data["status"], "rejected")
        self.assertEqual(self.balance("ACCOUNTS_PAYABLE"), D("-110.00"))
        self.assertEqual(self.c_nora.post(f"{BILLS}{a['id']}/void/", {}, format="json").status_code, 200)   # posted bills void as before
        self.assertEqual(self.c_dan.post(f"{BILLS}{b['id']}/void/", {}, format="json").data["code"], "not_posted")
        self.assertTrue(self.trial_balance_ok())

    def test_returns_are_not_held(self):
        require(self.bakery, expense=False)
        orig = self.bill(branch=self.north.id).data
        self.c_dan.post(f"{BILLS}{orig['id']}/approve/", {}, format="json")
        r = self.bill(kind="return", original=orig["id"], branch=self.north.id, items=[{"description": "Flour", "quantity": "1", "unit_price": "50", "tax_rate": "10"}])
        self.assertEqual(r.data["status"], "posted", r.content)

    def test_return_against_pending_original_is_refused(self):
        require(self.bakery, expense=False)
        orig = self.bill(branch=self.north.id).data
        r = self.bill(kind="return", original=orig["id"], branch=self.north.id)
        self.assertEqual(r.data["code"], "invalid_original")

    def test_period_lock_checked_at_approval(self):
        require(self.bakery, expense=False)
        b = self.bill(branch=self.north.id).data
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through=datetime.date(2026, 3, 31))
        r = self.c_dan.post(f"{BILLS}{b['id']}/approve/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get(f"{BILLS}{b['id']}/").data["status"], "pending")


class BillApprovalStockTests(CostBase):
    def test_stock_moves_only_when_a_stocked_bill_is_approved(self):
        self.client.post("/api/v1/inventory/costing/enable/", {"date": "2026-01-01", "costs": {}}, format="json")
        require(self.bakery, expense=False)
        r = self.buy(self.bread, 10, 20)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(self.bal("INVENTORY"), D("0"))
        a = self.client.post(f"{BILLS}{r.data['id']}/approve/", {}, format="json")      # owner approving own bill
        self.assertEqual(a.status_code, 200, a.content)
        self.assertEqual(self.bal("INVENTORY"), D("200"))
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assert_reconciled()


class BillableAndReceiptTests(ExpenseBase):
    def billable(self, **kw):
        return self.make(is_billable=True, customer=self.customer.id, **kw)

    def invoice(self, ids, client=None, **kw):
        body = {"party": self.customer.id, "date": "2026-03-15", "items": [{"description": "Work", "quantity": "1", "unit_price": "1000", "tax_rate": "0"}],
                "billable_expenses": ids, **kw}
        return (client or self.client).post("/api/v1/sales/invoices/", body, format="json")

    def test_billable_needs_a_customer(self):
        self.assertEqual(self.make(is_billable=True).data["code"], "invalid_customer")
        self.assertEqual(self.make(customer=self.customer.id).data["code"], "invalid_customer")
        self.assertEqual(self.make(is_billable=True, customer=self.supplier.id).data["code"], "invalid_customer")

    def test_billing_adds_a_line_at_cost_once_and_links_the_expense(self):
        e = self.billable().data                                           # total 110.00
        r = self.invoice([e["id"]])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(D(r.data["total"]), D("1110.00"))
        self.assertEqual(len(r.data["items"]), 2)
        self.assertEqual(self.client.get(f"{URL}{e['id']}/").data["billed_invoice"], r.data["id"])
        self.assertEqual(self.invoice([e["id"]]).data["code"], "already_billed")
        self.assertEqual(self.client.post(f"{URL}{e['id']}/void/", {}, format="json").data["code"], "expense_billed")
        self.assertTrue(self.trial_balance_ok())

    def test_voiding_the_invoice_releases_the_expense(self):
        e = self.billable().data
        inv = self.invoice([e["id"]]).data
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{inv['id']}/void/", {}, format="json").status_code, 200)
        self.assertIsNone(self.client.get(f"{URL}{e['id']}/").data["billed_invoice"])
        self.assertEqual(self.invoice([e["id"]]).status_code, 201)

    def test_wrong_customer_pending_unflagged_and_foreign_expenses_are_refused(self):
        other_cust = self.customer.__class__.objects.create(organization=self.bakery, name="Cust B", kind="customer")
        e = self.make(is_billable=True, customer=other_cust.id).data
        self.assertEqual(self.invoice([e["id"]]).data["code"], "invalid_billable")
        plain = self.make().data
        self.assertEqual(self.invoice([plain["id"]]).data["code"], "invalid_billable")
        require(self.bakery, bill=False)
        pend = self.billable().data
        self.assertEqual(pend["status"], "pending")
        self.assertEqual(self.invoice([pend["id"]]).data["code"], "invalid_billable")
        self.assertEqual(self.invoice([999999]).data["code"], "invalid_billable")

    def test_branch_scope_hides_other_branch_expenses(self):
        e = self.billable(branch=self.south).data
        r = self.invoice([e["id"]], client=self.c_nora, branch=self.north.id)
        self.assertEqual(r.data["code"], "invalid_billable")

    def test_up_to_five_receipts_with_type_checks_and_delete(self):
        from .test_expenses import PNG, MEDIA
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        e = self.make().data
        with override_settings(MEDIA_ROOT=MEDIA):
            for i in range(5):
                r = self.client.post(f"{URL}{e['id']}/receipts/", {"file": SimpleUploadedFile(f"r{i}.png", PNG, "image/png")}, format="multipart")
                self.assertEqual(r.status_code, 201, r.content)
            r = self.client.post(f"{URL}{e['id']}/receipts/", {"file": SimpleUploadedFile("x.png", PNG, "image/png")}, format="multipart")
            self.assertEqual(r.data["code"], "too_many_receipts")
            listed = self.client.get(f"{URL}{e['id']}/receipts/").data
            self.assertEqual(len(listed), 5)
            self.assertTrue(self.client.get(f"{URL}{e['id']}/").data["has_receipt"])
            got = self.client.get(f"{URL}{e['id']}/receipts/{listed[0]['id']}/")
            self.assertEqual(got.status_code, 200)
            self.assertEqual(self.client.delete(f"{URL}{e['id']}/receipts/{listed[0]['id']}/").status_code, 204)
            bad = self.client.post(f"{URL}{e['id']}/receipts/", {"file": SimpleUploadedFile("x.png", b"not an image", "image/png")}, format="multipart")
            self.assertEqual(bad.data["code"], "receipt_invalid_type")
            # another organisation cannot reach them; a viewer cannot add
            bob = self.as_user(self.bob, self.other)
            self.assertEqual(bob.get(f"{URL}{e['id']}/receipts/{listed[1]['id']}/").status_code, 404)
            r = self.c_vic.post(f"{URL}{e['id']}/receipts/", {"file": SimpleUploadedFile("x.png", PNG, "image/png")}, format="multipart")
            self.assertEqual(r.status_code, 403)
