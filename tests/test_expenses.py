"""Expenses: posting maths, validation, void, idempotency, period lock, branch access, org isolation, receipts."""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.ledger.models import Account, JournalLine, JournalTransaction
from apps.organizations.models import Organization

from .base import AccountingTestCase, D
from .test_branch_access import BranchBase

URL = "/api/v1/expenses/"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MEDIA = tempfile.mkdtemp(prefix="exp-media-")


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


class ExpenseBase(BranchBase):
    def setUp(self):
        super().setUp()
        self.fuel = Account.objects.create(organization=self.bakery, code="6100", name="Fuel", type="expense")
        self.other_fuel = Account.objects.create(organization=self.other, code="6100", name="Fuel", type="expense")

    def body(self, branch=None, amount="100", rate="10", incl=False, date="2026-03-10", **kw):
        return {"date": date, "expense_account": self.fuel.id, "paid_through": self.acct("CASH").id, "amount": amount,
                "tax_rate": rate, "amount_includes_tax": incl, "branch": branch.id if branch else None, **kw}

    def make(self, client=None, **kw):
        return (client or self.client).post(URL, self.body(**kw), format="json")


class ExpensePostingTests(ExpenseBase):
    def test_tax_exclusive_posts_balanced_entry(self):
        r = self.make()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["number"], "EXP-000001")
        self.assertEqual((D(r.data["subtotal"]), D(r.data["tax_total"]), D(r.data["total"])), (D("100.00"), D("10.00"), D("110.00")))
        self.assertEqual(self.balance("CASH"), D("-110.00"))
        self.assertEqual(self.balance("TAX_INPUT"), D("10.00"))
        self.assertEqual(JournalLine.objects.get(account=self.fuel).debit, D("100.00"))
        self.assertTrue(self.trial_balance_ok())

    def test_tax_inclusive_backs_tax_out(self):
        r = self.make(amount="110", incl=True)
        self.assertEqual((D(r.data["subtotal"]), D(r.data["tax_total"]), D(r.data["total"])), (D("100.00"), D("10.00"), D("110.00")))
        r = self.make(amount="100", rate="15", incl=True)  # 100 / 1.15 = 86.96, tax 13.04, total stays as entered
        self.assertEqual((D(r.data["subtotal"]), D(r.data["tax_total"]), D(r.data["total"])), (D("86.96"), D("13.04"), D("100.00")))
        self.assertTrue(self.trial_balance_ok())

    def test_zero_rate_has_no_tax_line(self):
        r = self.make(rate="0")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(JournalLine.objects.filter(account=self.acct("TAX_INPUT")).count(), 0)

    def test_numbers_increment_and_vendor_is_stored(self):
        self.make()
        r = self.make(vendor=self.supplier.id, reference="INV-9")
        self.assertEqual(r.data["number"], "EXP-000002")
        self.assertEqual((r.data["vendor_name"], r.data["reference"]), ("Supp A", "INV-9"))

    def test_validation(self):
        bad = [
            self.body(amount="0"), self.body(amount="-5"), self.body(rate="101"), self.body(rate="-1"),
            self.body(expense_account=self.acct("CASH").id),                 # not an expense account
            self.body(paid_through=self.fuel.id),                            # not cash/bank
            self.body(vendor=self.customer.id),                              # customer-only party
            self.body(expense_account=self.other_fuel.id),                   # other organization's account
            self.body(paid_through=Account.objects.get(organization=self.other, system_code="CASH").id),
            self.body(branch=self.foreign),                                  # other organization's branch
            self.body(branch=self.closed),                                   # inactive branch
        ]
        before = JournalTransaction.objects.count()
        for b in bad:
            if isinstance(b.get("branch"), type(self.north)):
                b["branch"] = b["branch"].id
            self.assertEqual(self.client.post(URL, b, format="json").status_code, 400, b)
        self.assertEqual(JournalTransaction.objects.count(), before)

    def test_inactive_accounts_refused(self):
        Account.objects.filter(pk=self.fuel.pk).update(is_active=False)
        self.assertEqual(self.make().status_code, 400)

    def test_void_reverses_and_cannot_repeat(self):
        e = self.make().data["id"]
        r = self.client.post(f"{URL}{e}/void/")
        self.assertEqual((r.status_code, r.data["status"]), (200, "void"))
        self.assertEqual(self.balance("CASH"), D("0"))
        self.assertEqual(self.balance("TAX_INPUT"), D("0"))
        self.assertEqual(self.client.post(f"{URL}{e}/void/").data["code"], "already_void")
        self.assertTrue(self.trial_balance_ok())

    def test_idempotency_key_returns_same_expense(self):
        a = self.make(idempotency_key="k1")
        b = self.make(idempotency_key="k1")
        self.assertEqual((a.status_code, b.status_code, a.data["id"]), (201, 200, b.data["id"]))
        self.assertEqual(JournalLine.objects.filter(account=self.fuel).count(), 1)

    def test_expense_appears_in_profit_loss_and_vat_input(self):
        self.make()
        pl = self.client.get("/api/v1/reports/profit-loss/", {"date_from": "2026-01-01", "date_to": "2026-12-31"}).data
        self.assertEqual(D(str(pl["total_expenses"])), D("100.00"))
        vat = self.client.get("/api/v1/reports/vat-summary/", {"date_from": "2026-01-01", "date_to": "2026-12-31"}).data
        self.assertEqual(D(str(vat["input_tax"]["net"])), D("10.00"))

    def test_list_filters_and_search(self):
        self.make(reference="ALPHA")
        self.make(reference="BETA", vendor=self.supplier.id)
        self.assertEqual(self.client.get(URL, {"search": "ALPHA"}).data["count"], 1)
        self.assertEqual(self.client.get(URL, {"vendor": self.supplier.id}).data["count"], 1)
        self.assertEqual(self.client.get(URL, {"expense_account": self.fuel.id, "date__gte": "2026-04-01"}).data["count"], 0)

    def test_viewer_cannot_create_or_void(self):
        e = self.make().data["id"]
        self.assertEqual(self.make(client=self.c_vic, branch=self.north).status_code, 403)
        self.assertEqual(self.c_vic.post(f"{URL}{e}/void/").status_code, 403)

    def test_expense_documents_are_immutable(self):
        e = self.make().data["id"]
        self.assertEqual(self.client.patch(f"{URL}{e}/", {"total": "1"}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{URL}{e}/").status_code, 405)


class ExpenseIsolationTests(ExpenseBase):
    def test_other_organization_cannot_see_or_void(self):
        e = self.make().data["id"]
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(URL).data["count"], 0)
        self.assertEqual(bob.get(f"{URL}{e}/").status_code, 404)
        self.assertEqual(bob.post(f"{URL}{e}/void/").status_code, 404)
        self.assertEqual(bob.get(f"{URL}{e}/receipt/").status_code, 404)

    def test_non_member_header_is_rejected(self):
        self.assertEqual(self.as_user(self.bob, self.bakery).get(URL).status_code, 403)


class ExpensePeriodLockTests(ExpenseBase):
    def test_locked_dates_block_create_and_void(self):
        e = self.make(date="2026-03-01").data["id"]
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through="2026-03-31")
        before = (JournalTransaction.objects.count(), JournalLine.objects.count())
        for d in ("2026-03-31", "2026-01-01"):
            r = self.make(date=d)
            self.assertEqual((r.status_code, r.data["code"]), (400, "period_locked"))
        self.assertEqual(self.client.post(f"{URL}{e}/void/").data["code"], "period_locked")
        self.assertEqual((JournalTransaction.objects.count(), JournalLine.objects.count()), before)
        self.assertEqual(self.make(date="2026-04-01").status_code, 201)
        self.assertEqual(self.client.get(URL).status_code, 200)  # history stays readable


class ExpenseBranchTests(ExpenseBase):
    def test_restricted_user_must_use_assigned_branch(self):
        self.assertEqual(self.make(client=self.c_nora, branch=self.north).status_code, 201)
        self.assertEqual(self.make(client=self.c_nora, branch=self.south).data["code"], "branch_forbidden")
        self.assertEqual(self.make(client=self.c_nora).data["code"], "branch_required")
        self.assertEqual(self.make(client=self.c_sam, branch=self.north).data["code"], "branch_forbidden")  # no assignments
        self.assertEqual(self.make(client=self.c_nora, branch=self.foreign).status_code, 400)

    def test_admin_may_post_any_branch_or_none(self):
        for b in (self.north, self.south, None):
            self.assertEqual(self.make(client=self.c_dan, branch=b).status_code, 201)
        self.assertEqual(self.make(client=self.client, branch=self.closed).data["code"], "inactive_branch")

    def test_register_detail_void_and_receipt_are_scoped(self):
        n = self.make(branch=self.north).data["id"]
        s = self.make(branch=self.south).data["id"]
        u = self.make().data["id"]  # unbranched history: admin only
        ids = lambda c: {x["id"] for x in c.get(URL).data["results"]}
        self.assertEqual(ids(self.c_nora), {n})
        self.assertEqual(ids(self.c_vic), {n})
        self.assertEqual(ids(self.c_sam), set())
        self.assertEqual(ids(self.client), {n, s, u})
        self.assertEqual(ids(self.c_dan), {n, s, u})
        for other in (s, u):
            self.assertEqual(self.c_nora.get(f"{URL}{other}/").status_code, 404)
            self.assertEqual(self.c_nora.post(f"{URL}{other}/void/").status_code, 404)
            self.assertEqual(self.c_nora.get(f"{URL}{other}/receipt/").status_code, 404)
        self.assertEqual(self.c_nora.post(f"{URL}{n}/void/").status_code, 200)

    def test_branch_totals_reconcile_with_organization(self):
        self.make(branch=self.north, amount="100")
        self.make(branch=self.south, amount="50", rate="0")
        self.make(amount="25", rate="0")
        q = {"date_from": "2026-01-01", "date_to": "2026-12-31"}
        pl = lambda **kw: D(str(self.client.get("/api/v1/reports/profit-loss/", {**q, **kw}).data["total_expenses"]))
        self.assertEqual(pl(), D("175.00"))
        self.assertEqual(pl(branch=self.north.id) + pl(branch=self.south.id), D("150.00"))
        self.assertEqual(D(str(self.c_nora.get("/api/v1/reports/profit-loss/", q).data["total_expenses"])), D("100.00"))


@override_settings(MEDIA_ROOT=MEDIA)
class ExpenseReceiptTests(ExpenseBase):
    def upload(self, client, eid, data=PNG, name="r.png", ctype="image/png"):
        return client.post(f"{URL}{eid}/receipt/", {"file": SimpleUploadedFile(name, data, content_type=ctype)}, format="multipart")

    def test_attach_and_download_roundtrip(self):
        e = self.make(branch=self.north).data["id"]
        self.assertEqual(self.client.get(f"{URL}{e}/receipt/").status_code, 404)
        r = self.upload(self.client, e)
        self.assertEqual((r.status_code, r.data["has_receipt"]), (200, True))
        d = self.c_nora.get(f"{URL}{e}/receipt/")
        self.assertEqual((d.status_code, b"".join(d.streaming_content)), (200, PNG))
        self.assertEqual(d["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.c_vic.get(f"{URL}{e}/receipt/").status_code, 200)   # viewers can read
        self.assertEqual(self.upload(self.c_vic, e).status_code, 403)             # but not write

    def test_file_name_is_not_trusted(self):
        e = self.make().data["id"]
        self.upload(self.client, e, name="../../evil.png")
        from apps.expenses.models import Expense
        self.assertTrue(Expense.objects.get(pk=e).receipt.name.startswith(f"expense-receipts/org{self.bakery.id}/EXP-"))

    def test_rejects_wrong_signature_type_size_and_missing(self):
        e = self.make().data["id"]
        self.assertEqual(self.upload(self.client, e, data=b"MZ-not-a-png", ctype="image/png").data["code"], "receipt_invalid_type")
        self.assertEqual(self.upload(self.client, e, data=PNG, name="x.exe", ctype="application/x-msdownload").data["code"], "receipt_invalid_type")
        self.assertEqual(self.upload(self.client, e, data=PNG + b"0" * (5 * 1024 * 1024)).data["code"], "receipt_too_large")
        self.assertEqual(self.client.post(f"{URL}{e}/receipt/", {}, format="multipart").data["code"], "receipt_missing")
        self.assertEqual(self.client.get(f"{URL}{e}/").data["has_receipt"], False)

    def test_replacing_a_receipt_keeps_a_single_file(self):
        e = self.make().data["id"]
        self.upload(self.client, e)
        self.upload(self.client, e, data=b"%PDF-1.4 x", name="r.pdf", ctype="application/pdf")
        d = self.client.get(f"{URL}{e}/receipt/")
        self.assertEqual(b"".join(d.streaming_content)[:4], b"%PDF")
