"""Credit refunds: cash back to a customer / from a supplier for credit held on an invoice."""
import datetime

from apps.ledger.models import JournalTransaction
from apps.organizations.models import Organization

from .base import D
from .test_branch_access import BranchBase
from .test_credit_application import CreditBase


class RefundTests(CreditBase):
    side = "sales"

    def refund(self, inv, amount="55", client=None, **kw):
        body = {"cash_account": self.acct("BANK").id, "amount": amount, "date": "2026-02-01", **kw}
        return (client or self.client).post(f"{self.base}{inv['id']}/refunds/", body, format="json")

    def test_refund_posts_balanced_entry_and_reduces_available_credit(self):
        inv = self.credit_invoice("110")            # paid 110, returned 110 -> credit 110
        before = self.balance("CASH"), self.balance("BANK")
        r = self.refund(inv, "40")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.data["number"].startswith("CRF-" if self.side == "sales" else "SRF-"))
        self.assertEqual(self.summary(inv)["credit_available"], D("70.00"))
        j = JournalTransaction.objects.get(source_type=f"treasury.refund_{'receipt' if self.side == 'sales' else 'payment'}")
        net = sum(l.debit - l.credit for l in j.lines.all())
        self.assertEqual(net, 0)
        bank_delta = self.balance("BANK") - before[1]
        self.assertEqual(bank_delta, D("-40") if self.side == "sales" else D("40"))
        self.assertTrue(self.trial_balance_ok())

    def test_cannot_refund_more_than_the_credit(self):
        inv = self.credit_invoice("110")
        self.assertEqual(self.refund(inv, "110.01").data["code"], "over_refund")
        self.assertEqual(self.refund(inv, "110").status_code, 201)
        self.assertEqual(self.refund(inv, "0.01").data["code"], "over_refund")
        self.assertEqual(self.refund(inv, "0").data["code"], "invalid_amount")

    def test_refund_and_credit_application_share_the_same_credit(self):
        inv = self.credit_invoice("110")
        other = self.doc(on="2026-01-12")
        self.refund(inv, "60")
        self.assertEqual(self.apply(inv, [(other, "51")]).data["code"], "over_applied")
        self.assertEqual(self.apply(inv, [(other, "50")]).status_code, 200)
        self.assertEqual(self.refund(inv, "1").data["code"], "over_refund")

    def test_an_invoice_with_no_credit_cannot_be_refunded(self):
        inv = self.doc()
        self.assertEqual(self.refund(inv, "10").data["code"], "over_refund")

    def test_idempotent_retry_posts_once(self):
        inv = self.credit_invoice("110")
        a = self.refund(inv, "30", idempotency_key="rf-1")
        b = self.refund(inv, "30", idempotency_key="rf-1")
        self.assertEqual((a.status_code, b.status_code, a.data["id"]), (201, 200, b.data["id"]))
        self.assertEqual(self.summary(inv)["credit_available"], D("80.00"))

    def test_date_rules_and_cash_account_rules(self):
        inv = self.credit_invoice("110")           # return dated 2026-01-20
        self.assertEqual(self.refund(inv, "10", date="2026-01-10").data["code"], "date_before_credit")
        self.assertEqual(self.refund(inv, "10", date="2999-01-01").data["code"], "future_date")
        self.assertEqual(self.refund(inv, "10", cash_account=self.acct("ACCOUNTS_PAYABLE").id).data["code"], "invalid_cash_account")
        foreign = self.acct("CASH", self.other).id
        self.assertEqual(self.refund(inv, "10", cash_account=foreign).status_code, 400)

    def test_period_lock_blocks_refund_and_void(self):
        inv = self.credit_invoice("110")
        r = self.refund(inv, "20", date="2026-02-01")
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through=datetime.date(2026, 2, 28))
        self.assertEqual(self.refund(inv, "5", date="2026-02-15").status_code, 400)
        v = self.client.post(f"{self.base}{inv['id']}/void-refund/", {"refund": r.data["id"]}, format="json")
        self.assertEqual(v.status_code, 400, v.content)
        self.assertEqual(self.summary(inv)["credit_available"], D("90.00"))

    def test_void_refund_restores_credit_and_ledger(self):
        inv = self.credit_invoice("110")
        base_bank = self.balance("BANK")
        r = self.refund(inv, "50")
        v = self.client.post(f"{self.base}{inv['id']}/void-refund/", {"refund": r.data["id"]}, format="json")
        self.assertEqual((v.status_code, v.data["status"]), (200, "void"))
        self.assertEqual(self.summary(inv)["credit_available"], D("110.00"))
        self.assertEqual(self.balance("BANK"), base_bank)
        self.assertEqual(self.client.post(f"{self.base}{inv['id']}/void-refund/", {"refund": r.data["id"]}, format="json").data["code"], "already_void")

    def test_voiding_the_return_or_invoice_is_blocked_while_a_refund_stands(self):
        inv = self.credit_invoice("110")
        self.refund(inv, "50")
        ret = self.client.get(f"{self.base}?kind=return").data["results"][0]
        r = self.client.post(f"{self.base}{ret['id']}/void/", {}, format="json")
        self.assertEqual(r.data["code"], "credit_applied")
        self.assertEqual(self.client.post(f"{self.base}{inv['id']}/void/", {}, format="json").status_code, 400)

    def test_viewer_and_other_org_are_refused(self):
        inv = self.credit_invoice("110")
        from django.contrib.auth import get_user_model
        from apps.organizations.models import Membership

        v = get_user_model().objects.create_user("viv", password="pw-12345-xx")
        Membership.objects.create(user=v, organization=self.bakery, role="viewer")
        self.assertEqual(self.refund(inv, "5", client=self.as_user(v, self.bakery)).status_code, 403)
        self.assertEqual(self.refund(inv, "5", client=self.as_user(self.bob, self.other)).status_code, 404)

    def test_refund_listed_and_reduces_outstanding_in_reports(self):
        inv = self.credit_invoice("110")
        self.refund(inv, "110")
        rows = self.client.get(f"{self.base}{inv['id']}/refunds/").data
        self.assertEqual([(x["amount"], x["status"]) for x in rows], [(D("110.00"), "posted")])
        self.assertEqual(self.client.get(f"{self.base}credits/").data, [])


class PurchaseRefundTests(RefundTests):
    side = "purchases"


class RefundBranchTests(BranchBase, CreditBase):
    side = "sales"

    def test_restricted_user_cannot_refund_other_branch_credit_and_refund_inherits_branch(self):
        inv = self.credit_invoice("110", branch=self.south.id, client=self.client)
        body = {"cash_account": self.acct("BANK").id, "amount": "10", "date": "2026-02-01"}
        r = self.c_nora.post(f"{self.base}{inv['id']}/refunds/", body, format="json")      # nora only has North
        self.assertEqual(r.status_code, 404)
        ok = self.c_dan.post(f"{self.base}{inv['id']}/refunds/", body, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        j = JournalTransaction.objects.get(source_type="treasury.refund_receipt")
        self.assertEqual(j.branch_id, self.south.id)
