"""Opening balances (voucher type 'opening') and fiscal-year close."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model

from apps.ledger.models import Account, JournalLine, JournalTransaction
from apps.organizations.fiscal import fiscal_year_bounds, is_fiscal_year_end, previous_year_end
from apps.organizations.models import FiscalYearClose, Membership, Organization

from .base import AccountingTestCase, D

User = get_user_model()
V = "/api/v1/vouchers/"


class OpeningBase(AccountingTestCase):
    def opening(self, client=None, date_="2026-01-01", lines=None, **kw):
        lines = lines if lines is not None else [
            {"account": self.acct("BANK").id, "debit": "1000"},
            {"account": self.acct("ACCOUNTS_RECEIVABLE").id, "debit": "500", "party": self.customer.id},
            {"account": self.acct("ACCOUNTS_PAYABLE").id, "credit": "300", "party": self.supplier.id},
        ]
        return (client or self.client).post(V, {"type": "opening", "date": date_, "lines": lines, **kw}, format="json")

    def member(self, name, role, org=None):
        u = User.objects.create_user(name, password="pw-12345-xx")
        Membership.objects.create(user=u, organization=org or self.bakery, role=role)
        return self.as_user(u, org or self.bakery)


class OpeningBalanceTests(OpeningBase):
    def test_posts_with_offset_to_a_new_equity_account(self):
        r = self.opening()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["number"], "OB-000001")
        off = Account.objects.get(organization=self.bakery, system_code="OPENING_OFFSET")
        self.assertEqual((off.type, off.code), ("equity", "3900"))
        self.assertEqual(self.balance("BANK"), D("1000"))
        net = JournalLine.objects.get(account=off)
        self.assertEqual((net.debit, net.credit), (D("0"), D("1200")))  # 1500 debits - 300 credits
        self.assertTrue(self.trial_balance_ok())
        bs = self.client.get("/api/v1/reports/balance-sheet/", {"as_of": "2026-01-31"}).data
        self.assertTrue(bs["balanced"])
        self.assertEqual(D(str(bs["total_assets"])), D("1500"))

    def test_party_balances_carry_forward(self):
        self.opening()
        rows = {x["party"]: x for x in self.client.get("/api/v1/party-balances/").data}
        self.assertEqual(D(str(rows[self.customer.id]["receivable"])), D("500"))
        self.assertEqual(D(str(rows[self.supplier.id]["payable"])), D("300"))

    def test_balanced_input_creates_no_offset_line(self):
        r = self.opening(lines=[{"account": self.acct("BANK").id, "debit": "50"}, {"account": self.acct("EQUITY").id, "credit": "50"}])
        self.assertEqual(r.status_code, 201)
        self.assertFalse(Account.objects.filter(organization=self.bakery, system_code="OPENING_OFFSET").exists())

    def test_offset_account_is_reused_and_picks_a_free_code(self):
        Account.objects.create(organization=self.bakery, code="3900", name="Taken", type="equity")
        self.opening()
        self.assertEqual(Account.objects.get(organization=self.bakery, system_code="OPENING_OFFSET").code, "3901")
        self.client.post(f"{V}{Voucher_id(self)}/void/")
        self.assertEqual(self.opening().status_code, 201)
        self.assertEqual(Account.objects.filter(organization=self.bakery, system_code="OPENING_OFFSET").count(), 1)

    def test_validation(self):
        bank, ar, rev = self.acct("BANK").id, self.acct("ACCOUNTS_RECEIVABLE").id, self.acct("SALES").id
        cases = [
            [{"account": bank, "debit": "10"}, {"account": rev, "credit": "10"}],            # profit-and-loss account
            [{"account": bank, "debit": "10"}, {"account": ar, "credit": "10"}],             # control account without a party
            [{"account": bank, "debit": "10", "credit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}],
        ]
        before = JournalTransaction.objects.count()
        for lines in cases:
            self.assertEqual(self.opening(lines=lines).status_code, 400, lines)
        self.assertEqual(JournalTransaction.objects.count(), before)

    def test_only_one_live_set_and_void_allows_redo(self):
        a = self.opening()
        self.assertEqual(self.opening().data["code"], "opening_exists")
        self.assertEqual(self.client.post(f"{V}{a.data['id']}/void/").status_code, 200)
        self.assertEqual(self.balance("BANK"), D("0"))
        self.assertEqual(self.opening().status_code, 201)

    def test_must_not_follow_existing_transactions(self):
        self.assertEqual(self.sale(date="2026-02-10").status_code, 201)
        r = self.opening(date_="2026-03-01")
        self.assertEqual((r.status_code, r.data["code"]), (400, "opening_after_transactions"))
        self.assertEqual(self.opening(date_="2026-02-10").status_code, 201)   # same day is allowed
        self.assertEqual(self.opening(date_="2026-01-31").data["code"], "opening_exists")

    def test_period_lock_applies(self):
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through="2026-01-31")
        self.assertEqual(self.opening(date_="2026-01-01").data["code"], "period_locked")

    def test_roles(self):
        for role in ("accountant", "viewer"):
            c = self.member(f"u_{role}", role)
            self.assertEqual(self.opening(client=c).status_code, 403, role)
        self.assertEqual(self.member("adm", "admin").post(V, {"type": "opening", "date": "2026-01-01", "lines": [
            {"account": self.acct("BANK").id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}]}, format="json").status_code, 201)

    def test_organization_isolation(self):
        other_bank = Account.objects.get(organization=self.other, system_code="BANK")
        r = self.opening(lines=[{"account": other_bank.id, "debit": "5"}, {"account": self.acct("EQUITY").id, "credit": "5"}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.opening().status_code, 201)
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.get(V, {"type": "opening"}).data["count"], 0)
        self.assertEqual(self.opening(client=bob, lines=[{"account": other_bank.id, "debit": "7"}, {"account": Account.objects.get(organization=self.other, system_code="EQUITY").id, "credit": "7"}]).status_code, 201)

    def test_profit_and_loss_ignores_opening(self):
        self.opening()
        pl = self.client.get("/api/v1/reports/profit-loss/", {"date_from": "2026-01-01", "date_to": "2026-12-31"}).data
        self.assertEqual((D(str(pl["total_income"])), D(str(pl["total_expenses"]))), (D("0"), D("0")))


def Voucher_id(test):
    from apps.vouchers.models import Voucher

    return Voucher.objects.get(organization=test.bakery, type="opening", status="posted").id


class FiscalHelperTests(AccountingTestCase):
    def test_bounds(self):
        self.assertEqual(fiscal_year_bounds(date(2026, 6, 15), 1), (date(2026, 1, 1), date(2026, 12, 31)))
        self.assertEqual(fiscal_year_bounds(date(2026, 2, 1), 4), (date(2025, 4, 1), date(2026, 3, 31)))
        self.assertEqual(fiscal_year_bounds(date(2026, 4, 1), 4), (date(2026, 4, 1), date(2027, 3, 31)))
        self.assertTrue(is_fiscal_year_end(date(2026, 3, 31), 4))
        self.assertFalse(is_fiscal_year_end(date(2026, 12, 31), 4))
        self.assertEqual(previous_year_end(date(2026, 4, 1), 4), date(2026, 3, 31))


@patch("django.utils.timezone.localdate", lambda *a, **k: date(2027, 2, 10))
class FiscalCloseTests(OpeningBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.sale(qty="1", price="1000", tax="0", date="2026-03-10").status_code, 201)
        self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-05-05",
                         "items": [{"description": "x", "quantity": "1", "unit_price": "300", "tax_rate": "0"}]}, format="json")
        self.assertEqual(self.sale(qty="1", price="200", tax="0", date="2027-01-20").status_code, 201)

    def url(self, name, org=None):
        return f"/api/v1/organizations/{(org or self.bakery).id}/{name}/"

    def test_status_names_next_year(self):
        r = self.client.get(self.url("fiscal-years")).data
        self.assertEqual((r["next_year_end"], r["closes"]), ("2026-12-31" if isinstance(r["next_year_end"], str) else date(2026, 12, 31), []))

    def test_close_locks_records_profit_and_posts_nothing(self):
        n = (JournalTransaction.objects.count(), JournalLine.objects.count())
        r = self.client.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((D(str(r.data["net_profit"])), r.data["year_start"], r.data["closed_by_name"]), (D("700"), "2026-01-01", "alice"))
        self.assertEqual((JournalTransaction.objects.count(), JournalLine.objects.count()), n)   # no closing journal
        self.assertEqual(Organization.objects.get(pk=self.bakery.pk).books_locked_through, date(2026, 12, 31))
        self.assertEqual(self.sale(date="2026-12-31").data["code"], "period_locked")
        self.assertEqual(self.sale(date="2027-01-01").status_code, 201)
        pl = self.client.get("/api/v1/reports/profit-loss/", {"date_from": "2026-01-01", "date_to": "2026-12-31"}).data
        self.assertEqual(D(str(pl["net_profit"])), D("700"))                                       # 2026 still readable
        self.assertEqual(self.client.get(self.url("fiscal-years")).data["closes"][0]["year_end"], "2026-12-31")
        self.assertEqual(self.client.get(self.url("fiscal-years")).data["next_year_end"], None)    # 2027 has not ended

    def test_rejections(self):
        post = lambda d: self.client.post(self.url("close-year"), {"year_end": d}, format="json")
        self.assertEqual(post("2026-11-30").data["code"], "not_year_end")
        self.assertEqual(post("2027-12-31").data["code"], "year_not_ended")
        self.assertEqual(post("nonsense").status_code, 400)
        self.assertEqual(self.client.post(self.url("close-year"), {}, format="json").status_code, 400)
        self.assertEqual(post("2026-12-31").status_code, 201)
        self.assertEqual(post("2026-12-31").data["code"], "already_closed")
        self.assertEqual(FiscalYearClose.objects.count(), 1)

    def test_years_close_in_order(self):
        with patch("django.utils.timezone.localdate", lambda *a, **k: date(2028, 2, 10)):
            r = self.client.post(self.url("close-year"), {"year_end": "2027-12-31"}, format="json")
            self.assertEqual((r.status_code, r.data["code"]), (400, "close_in_order"))
            self.assertEqual(self.client.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json").status_code, 201)
            self.assertEqual(self.client.post(self.url("close-year"), {"year_end": "2027-12-31"}, format="json").status_code, 201)

    def test_roles_and_isolation(self):
        for role in ("accountant", "viewer"):
            c = self.member(f"c_{role}", role)
            self.assertEqual(c.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json").status_code, 403, role)
            self.assertEqual(c.get(self.url("fiscal-years")).status_code, 200)
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json").status_code, 404)
        self.assertEqual(bob.get(self.url("fiscal-years")).status_code, 404)
        self.assertIsNone(Organization.objects.get(pk=self.bakery.pk).books_locked_through)

    def test_april_fiscal_year(self):
        Organization.objects.filter(pk=self.bakery.pk).update(fiscal_year_start_month=4)
        r = self.client.get(self.url("fiscal-years")).data
        self.assertEqual(str(r["next_year_end"]), "2026-03-31")
        self.assertEqual(self.client.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json").data["code"], "not_year_end")
        c = self.client.post(self.url("close-year"), {"year_end": "2026-03-31"}, format="json")
        self.assertEqual((c.status_code, c.data["year_start"], D(str(c.data["net_profit"]))), (201, "2025-04-01", D("1000")))

    def test_balance_sheet_splits_prior_and_current_year_profit(self):
        bs = self.client.get("/api/v1/reports/balance-sheet/", {"as_of": "2027-01-31"}).data
        self.assertEqual(D(str(bs["retained_earnings_prior_years"])), D("700"))
        self.assertEqual(D(str(bs["current_year_earnings"])), D("200"))
        self.assertEqual(D(str(bs["current_period_earnings"])), D("900"))
        self.assertTrue(bs["balanced"])
        mid = self.client.get("/api/v1/reports/balance-sheet/", {"as_of": "2026-12-31"}).data
        self.assertEqual((D(str(mid["retained_earnings_prior_years"])), D(str(mid["current_year_earnings"]))), (D("0"), D("700")))
        self.assertTrue(mid["balanced"])

    def test_lock_can_still_be_reopened_by_admin(self):
        self.client.post(self.url("close-year"), {"year_end": "2026-12-31"}, format="json")
        patch_none = lambda **kw: self.client.patch(f"/api/v1/organizations/{self.bakery.id}/", {"books_locked_through": None, **kw}, format="json")
        self.assertEqual(patch_none().status_code, 400)                                    # needs a reason...
        self.assertEqual(patch_none(lock_change_reason="redo").data["code"], "fiscal_year_closed")   # ...and a closed year has its own reopen step
        r = self.client.post(self.url("reopen-year"), {"year_end": "2026-12-31", "reason": "redo"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.sale(date="2026-12-31").status_code, 201)
