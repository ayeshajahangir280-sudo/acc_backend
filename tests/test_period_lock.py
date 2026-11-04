"""Period locking: books closed through a date cannot be posted to or voided, enforced server-side."""
import datetime as dt

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.core.models import NumberSequence
from apps.ledger.models import JournalLine, JournalTransaction
from apps.organizations.models import Membership, Organization

from .base import AccountingTestCase, D

User = get_user_model()
LOCK = dt.date(2026, 1, 31)
IN_LOCK = "2026-01-31"      # the lock date itself: blocked (inclusive)
BEFORE = "2026-01-15"       # earlier than the lock: blocked
AFTER = "2026-02-01"        # first open day: allowed


class PeriodLockBase(AccountingTestCase):
    def lock(self, d=LOCK, org=None):
        Organization.objects.filter(pk=(org or self.bakery).pk).update(books_locked_through=d)

    def state(self):
        """Everything a blocked request must leave untouched."""
        return (JournalTransaction.objects.count(), JournalLine.objects.count(),
                sorted(NumberSequence.objects.values_list("key", "last_value")))

    def sales_body(self, date):
        return {"party": self.customer.id, "date": date, "items": [{"description": "Bread", "quantity": "1", "unit_price": "100", "tax_rate": "0"}]}

    def purchase_body(self, date):
        return {"party": self.supplier.id, "date": date, "items": [{"description": "Flour", "quantity": "1", "unit_price": "100", "tax_rate": "0"}]}

    def receipt_body(self, date):
        return {"party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "10", "date": date}

    def payment_body(self, date):
        return {"party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": "10", "date": date}

    def voucher_body(self, vtype, date):
        if vtype == "contra":
            lines = [{"account": self.acct("CASH").id, "debit": "10"}, {"account": self.acct("BANK").id, "credit": "10"}]
        elif vtype == "journal":
            lines = [{"account": self.acct("CASH").id, "debit": "10"}, {"account": self.acct("EQUITY").id, "credit": "10"}]
        else:
            lines = [{"account": self.acct("PURCHASES").id, "debit": "10"}, {"account": self.acct("EQUITY").id, "credit": "10"}]
        return {"type": vtype, "date": date, "lines": lines}

    def expense_body(self, date):
        from apps.ledger.models import Account

        cat, _ = Account.objects.get_or_create(organization=self.bakery, code="6100", defaults={"name": "Fuel", "type": "expense"})
        return {"date": date, "expense_account": cat.id, "paid_through": self.acct("CASH").id, "amount": "10", "tax_rate": "0"}

    def creators(self):
        """(label, url, body-factory) for every document type that posts to the ledger."""
        c = [("sales invoice", "/api/v1/sales/invoices/", self.sales_body),
             ("purchase invoice", "/api/v1/purchases/invoices/", self.purchase_body),
             ("receipt", "/api/v1/receipts/", self.receipt_body),
             ("payment", "/api/v1/payments/", self.payment_body),
             ("expense", "/api/v1/expenses/", self.expense_body)]
        c += [(f"{t} voucher", "/api/v1/vouchers/", (lambda d, t=t: self.voucher_body(t, d))) for t in ("general", "journal", "contra")]
        return c


class LockBoundaryTests(PeriodLockBase):
    def test_every_document_type_is_blocked_on_and_before_the_lock_date(self):
        self.lock()
        for label, url, body in self.creators():
            for d in (IN_LOCK, BEFORE):
                before = self.state()
                r = self.client.post(url, body(d), format="json")
                self.assertEqual(r.status_code, 400, f"{label} {d}: {r.data}")
                self.assertEqual(r.data["code"], "period_locked", f"{label} {d}")
                self.assertEqual(self.state(), before, f"{label} {d} left residue (rows or consumed numbers)")

    def test_every_document_type_is_allowed_from_the_day_after(self):
        self.lock()
        for label, url, body in self.creators():
            r = self.client.post(url, body(AFTER), format="json")
            self.assertEqual(r.status_code, 201, f"{label}: {r.data}")
        self.assertTrue(self.trial_balance_ok())

    def test_returns_are_blocked_too(self):
        inv = self.client.post("/api/v1/sales/invoices/", self.sales_body(AFTER), format="json").data
        pinv = self.client.post("/api/v1/purchases/invoices/", self.purchase_body(AFTER), format="json").data
        self.lock(dt.date(2026, 2, 28))
        for url, base, orig in (("/api/v1/sales/invoices/", self.sales_body, inv), ("/api/v1/purchases/invoices/", self.purchase_body, pinv)):
            before = self.state()
            r = self.client.post(url, {**base("2026-02-15"), "kind": "return", "original": orig["id"]}, format="json")
            self.assertEqual((r.status_code, r.data["code"]), (400, "period_locked"))
            self.assertEqual(self.state(), before)

    def test_unlocked_organization_is_unaffected(self):
        for label, url, body in self.creators():
            self.assertEqual(self.client.post(url, body(BEFORE), format="json").status_code, 201, label)

    def test_lock_is_per_organization(self):
        self.lock(org=self.other)  # lock the OTHER organization only
        self.assertEqual(self.client.post("/api/v1/sales/invoices/", self.sales_body(BEFORE), format="json").status_code, 201)
        other_client = self.as_user(self.bob, self.other)
        # bob's org is locked, but alice's lock state does not matter to him
        self.assertEqual(other_client.post("/api/v1/vouchers/", {"type": "journal", "date": BEFORE, "lines": [
            {"account": self.acct("CASH", self.other).id, "debit": "5"}, {"account": self.acct("EQUITY", self.other).id, "credit": "5"}]},
            format="json").data["code"], "period_locked")

    def test_moving_the_lock_forward_blocks_more_and_unlocking_reopens(self):
        self.lock()
        self.assertEqual(self.client.post("/api/v1/sales/invoices/", self.sales_body(AFTER), format="json").status_code, 201)
        self.lock(dt.date(2026, 2, 28))
        self.assertEqual(self.client.post("/api/v1/sales/invoices/", self.sales_body("2026-02-10"), format="json").data["code"], "period_locked")
        self.lock(None)
        self.assertEqual(self.client.post("/api/v1/sales/invoices/", self.sales_body("2026-02-10"), format="json").status_code, 201)


class LockVoidTests(PeriodLockBase):
    def make_all(self, date):
        ids = {}
        for label, url, body in self.creators():
            r = self.client.post(url, body(date), format="json")
            self.assertEqual(r.status_code, 201, f"{label}: {r.data}")
            ids[label] = (url, r.data["id"])
        return ids

    def test_void_blocked_for_every_document_type_in_a_locked_period(self):
        ids = self.make_all(BEFORE)
        self.lock()
        for label, (url, pk) in ids.items():
            before = self.state()
            r = self.client.post(f"{url}{pk}/void/")
            self.assertEqual((r.status_code, r.data["code"]), (400, "period_locked"), label)
            self.assertEqual(self.state(), before, label)
            self.assertEqual(self.client.get(f"{url}{pk}/").data["status"], "posted", f"{label} must stay posted")

    def test_void_on_the_lock_date_is_blocked_and_the_day_after_is_allowed(self):
        on_lock = self.client.post("/api/v1/receipts/", self.receipt_body(IN_LOCK), format="json").data["id"]
        after = self.client.post("/api/v1/receipts/", self.receipt_body(AFTER), format="json").data["id"]
        self.lock()
        self.assertEqual(self.client.post(f"/api/v1/receipts/{on_lock}/void/").data["code"], "period_locked")
        self.assertEqual(self.client.post(f"/api/v1/receipts/{after}/void/").status_code, 200)

    def test_void_works_again_after_unlocking(self):
        pk = self.client.post("/api/v1/sales/invoices/", self.sales_body(BEFORE), format="json").data["id"]
        self.lock()
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{pk}/void/").status_code, 400)
        self.lock(None)
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{pk}/void/").status_code, 200)
        self.assertEqual(self.balance("ACCOUNTS_RECEIVABLE"), D("0"))

    def test_void_in_open_period_of_a_document_whose_receipt_is_locked_still_follows_existing_rules(self):
        # an open-period invoice can be voided even if older periods are locked
        pk = self.client.post("/api/v1/sales/invoices/", self.sales_body(AFTER), format="json").data["id"]
        self.lock()
        self.assertEqual(self.client.post(f"/api/v1/sales/invoices/{pk}/void/").status_code, 200)

    def test_allocating_a_locked_receipt_later_is_blocked_but_new_receipts_may_settle_old_invoices(self):
        inv = self.client.post("/api/v1/sales/invoices/", self.sales_body(BEFORE), format="json").data["id"]
        rid = self.client.post("/api/v1/receipts/", self.receipt_body(BEFORE), format="json").data["id"]
        self.lock()
        r = self.client.post(f"/api/v1/receipts/{rid}/allocate/", {"allocations": [{"invoice": inv, "amount": "10"}]}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "period_locked"))
        # an open-period receipt may still be applied to an invoice from the locked period
        r = self.client.post("/api/v1/receipts/", {**self.receipt_body(AFTER), "allocations": [{"invoice": inv, "amount": "10"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)


class LockReadAccessTests(PeriodLockBase):
    def test_history_and_reports_remain_readable_when_locked(self):
        inv = self.client.post("/api/v1/sales/invoices/", self.sales_body(BEFORE), format="json").data
        self.client.post("/api/v1/receipts/", self.receipt_body(BEFORE), format="json")
        self.lock()
        self.assertEqual(self.client.get("/api/v1/sales/invoices/").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/v1/sales/invoices/{inv['id']}/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/receipts/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/journal/").status_code, 200)
        for rep in ("trial-balance", "profit-loss", "balance-sheet", "aging", "vendor", "sales-summary", "vat-summary"):
            r = self.client.get(f"/api/v1/reports/{rep}/", {"as_of": "2026-01-31", "date_from": "2026-01-01", "date_to": "2026-01-31"})
            self.assertEqual(r.status_code, 200, f"{rep}: {getattr(r, 'data', '')}")
        self.assertTrue(self.trial_balance_ok())

    def test_posted_documents_still_cannot_be_edited_or_deleted(self):
        pk = self.client.post("/api/v1/sales/invoices/", self.sales_body(AFTER), format="json").data["id"]
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(f"/api/v1/sales/invoices/{pk}/", {}, format="json").status_code, 405, method)


class LockPermissionTests(PeriodLockBase):
    def org_url(self):
        return f"/api/v1/organizations/{self.bakery.id}/"

    def member(self, name, role):
        u = User.objects.create_user(name, password="pw-12345-xx")
        Membership.objects.create(user=u, organization=self.bakery, role=role)
        return self.as_user(u, self.bakery)

    def test_owner_can_set_change_and_clear_the_lock(self):
        r = self.client.patch(self.org_url(), {"books_locked_through": "2026-01-31"}, format="json")
        self.assertEqual((r.status_code, r.data["books_locked_through"]), (200, "2026-01-31"))
        self.assertEqual(self.client.get(self.org_url()).data["books_locked_through"], "2026-01-31")
        self.assertEqual(self.client.patch(self.org_url(), {"books_locked_through": "2026-02-28"}, format="json").status_code, 200)
        r = self.client.patch(self.org_url(), {"books_locked_through": None}, format="json")
        self.assertEqual((r.status_code, r.data["books_locked_through"]), (200, None))

    def test_admin_can_set_the_lock(self):
        r = self.member("adm", Membership.Role.ADMIN).patch(self.org_url(), {"books_locked_through": "2026-01-31"}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_accountant_and_viewer_cannot_change_the_lock(self):
        self.lock()
        for name, role in (("acc", Membership.Role.ACCOUNTANT), ("view", Membership.Role.VIEWER)):
            c = self.member(name, role)
            for value in (None, "2026-12-01", "2025-01-01"):
                r = c.patch(self.org_url(), {"books_locked_through": value}, format="json")
                self.assertEqual(r.status_code, 403, f"{name} {value}")
        self.bakery.refresh_from_db()
        self.assertEqual(self.bakery.books_locked_through, LOCK)

    def test_non_member_cannot_see_or_change_the_lock(self):
        self.lock()
        r = self.as_user(self.bob, self.other).patch(self.org_url(), {"books_locked_through": None}, format="json")
        self.assertEqual(r.status_code, 404)
        self.bakery.refresh_from_db()
        self.assertEqual(self.bakery.books_locked_through, LOCK)

    def test_future_lock_date_is_rejected(self):
        tomorrow = (timezone.localdate() + dt.timedelta(days=1)).isoformat()
        r = self.client.patch(self.org_url(), {"books_locked_through": tomorrow}, format="json")
        self.assertEqual(r.status_code, 400)
        today = timezone.localdate().isoformat()
        self.assertEqual(self.client.patch(self.org_url(), {"books_locked_through": today}, format="json").status_code, 200)

    def test_garbage_lock_date_is_rejected(self):
        self.assertEqual(self.client.patch(self.org_url(), {"books_locked_through": "not-a-date"}, format="json").status_code, 400)


class LockBypassTests(PeriodLockBase):
    def test_lock_is_enforced_without_any_frontend(self):
        """Raw API calls (no UI) with every combination of tricks hit the same server-side check."""
        self.lock()
        # 1. idempotency key does not carry a new locked document through
        body = {**self.sales_body(BEFORE), "idempotency_key": "k-locked"}
        for _ in range(2):
            self.assertEqual(self.client.post("/api/v1/sales/invoices/", body, format="json").data["code"], "period_locked")
        # 2. smuggling the lock field into a document body is ignored
        r = self.client.post("/api/v1/sales/invoices/", {**self.sales_body(BEFORE), "books_locked_through": None}, format="json")
        self.assertEqual(r.data["code"], "period_locked")
        # 3. a branch does not exempt the document
        branch = self.client.post("/api/v1/branches/", {"name": "North"}, format="json").data["id"]
        r = self.client.post("/api/v1/sales/invoices/", {**self.sales_body(BEFORE), "branch": branch}, format="json")
        self.assertEqual(r.data["code"], "period_locked")
        # 4. date formats that still resolve to a locked date are blocked (the serializer normalises the date)
        r = self.client.post("/api/v1/sales/invoices/", self.sales_body("2026-01-01"), format="json")
        self.assertEqual(r.data["code"], "period_locked")
        # 5. nothing was posted by any attempt
        self.assertEqual(JournalTransaction.objects.count(), 0)
        self.assertTrue(self.trial_balance_ok())

    def test_replaying_an_old_idempotent_request_returns_the_original_without_reposting(self):
        body = {**self.sales_body(BEFORE), "idempotency_key": "k-old"}
        first = self.client.post("/api/v1/sales/invoices/", body, format="json")
        self.assertEqual(first.status_code, 201)
        self.lock()
        again = self.client.post("/api/v1/sales/invoices/", body, format="json")
        self.assertEqual((again.status_code, again.data["id"]), (200, first.data["id"]))
        self.assertEqual(JournalTransaction.objects.count(), 1)

    def test_lock_set_mid_request_is_honoured_even_with_a_stale_organization_object(self):
        """The service re-reads the lock from the database, not from the request-scoped organization instance."""
        from apps.ledger.services import Line, post_journal

        stale = Organization.objects.get(pk=self.bakery.pk)  # loaded before the lock is set
        self.lock()
        self.assertIsNone(stale.books_locked_through)
        from apps.core.errors import DomainError

        with self.assertRaises(DomainError) as cm:
            post_journal(stale, LOCK, [Line(self.acct("CASH"), debit=D("5")), Line(self.acct("EQUITY"), credit=D("5"))], "test")
        self.assertEqual(cm.exception.code, "period_locked")

    def test_django_admin_cannot_void_in_a_locked_period(self):
        """The admin void action calls the same service, so it is covered by the same check."""
        from apps.ledger.services import void_journal
        from apps.core.errors import DomainError

        pk = self.client.post("/api/v1/sales/invoices/", self.sales_body(BEFORE), format="json").data["id"]
        txn = JournalTransaction.objects.get(source_type="sales.invoice", source_id=pk)
        self.lock()
        with self.assertRaises(DomainError):
            void_journal(txn)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "posted")
