"""Sub-account roll-ups in trial balance, profit and loss and balance sheet."""
from apps.ledger.models import Account
from apps.ledger.services import Line, post_journal

from .base import AccountingTestCase, D


class RollupTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        mk = lambda code, name, type, parent=None: Account.objects.create(organization=self.bakery, code=code, name=name, type=type, parent=parent)
        self.exp = mk("6300", "Utilities", "expense")
        self.elec = mk("6301", "Electricity", "expense", self.exp)
        self.solar = mk("6302", "Solar", "expense", self.elec)          # three levels
        self.water = mk("6303", "Water", "expense", self.exp)
        self.inc = mk("4900", "Other sales", "income")
        self.inc_sub = mk("4901", "Cakes", "income", self.inc)
        self.cash = self.acct("CASH")
        n = [0]

        def post(dr, cr, amt, on="2026-02-10"):
            n[0] += 1
            post_journal(self.bakery, on, [Line(dr, debit=D(amt)), Line(cr, credit=D(amt))], "t", n[0], f"T{n[0]}", "", None, self.alice)
        post(self.exp, self.cash, "10")
        post(self.elec, self.cash, "20")
        post(self.solar, self.cash, "5")
        post(self.water, self.cash, "7")
        post(self.cash, self.inc_sub, "100")      # activity only on a sub-account: the parent must still appear
        self.post = post

    def rows(self, url, key=None, **q):
        d = self.client.get(f"/api/v1/reports/{url}/", q).data
        return d if key is None else {r["code"]: r for r in d[key]}

    def test_profit_loss_rollup_and_totals_not_double_counted(self):
        d = self.rows("profit-loss")
        ex = {r["code"]: r for r in d["expenses"]}
        self.assertEqual([r["code"] for r in d["expenses"]], ["6300", "6301", "6302", "6303"])      # tree order
        self.assertEqual([r["depth"] for r in d["expenses"]], [0, 1, 2, 1])
        self.assertEqual((ex["6300"]["amount"], ex["6300"]["rollup_amount"]), (D("10"), D("42")))
        self.assertEqual(ex["6301"]["rollup_amount"], D("25"))
        self.assertEqual(ex["6302"]["rollup_amount"], D("5"))
        self.assertEqual(D(str(d["total_expenses"])), D("42"))                                       # own amounts, once each
        inc = {r["code"]: r for r in d["income"]}
        self.assertEqual((inc["4900"]["amount"], inc["4900"]["rollup_amount"]), (D("0"), D("100")))  # parent shown, no own postings
        self.assertEqual(D(str(d["net_profit"])), D("58"))

    def test_trial_balance_signs_and_totals(self):
        d = self.rows("trial-balance")
        tb = {r["code"]: r for r in d["rows"]}
        self.assertEqual((tb["6300"]["debit"], tb["6300"]["rollup_debit"]), (D("10"), D("42")))
        self.assertEqual((tb["4900"]["credit"], tb["4900"]["rollup_credit"]), (D("0"), D("100")))     # credit-normal sign kept
        self.assertEqual(tb["4901"]["parent"], self.inc.id)
        self.assertEqual(D(str(d["total_debit"])), D(str(d["total_credit"])))
        self.assertTrue(d["balanced"])

    def test_trial_balance_rollup_net_when_children_offset(self):
        self.post(self.cash, self.water, "3")   # water now 4 net debit; make a credit balance on a child
        self.post(self.cash, self.solar, "20")  # solar: 5 - 20 = -15 net credit
        d = self.rows("trial-balance")
        tb = {r["code"]: r for r in d["rows"]}
        self.assertEqual((tb["6302"]["debit"], tb["6302"]["credit"]), (D("0"), D("15")))
        self.assertEqual(tb["6301"]["rollup_debit"], D("5"))       # 20 + (5-20) = 5
        self.assertEqual(tb["6300"]["rollup_debit"], D("19"))      # 10 + 20 - 15 + 4
        self.assertTrue(d["balanced"])

    def test_balance_sheet_equity_and_balance(self):
        sub = Account.objects.create(organization=self.bakery, code="1850", name="Bank group", type="asset")
        child = Account.objects.create(organization=self.bakery, code="1851", name="Bank A", type="asset", parent=sub)
        self.post(child, self.inc, "40")
        d = self.rows("balance-sheet")
        a = {r["code"]: r for r in d["assets"]}
        self.assertEqual((a["1850"]["amount"], a["1850"]["rollup_amount"]), (D("0"), D("40")))
        self.assertTrue(d["balanced"])
        self.assertEqual(D(str(d["total_assets"])), D(str(d["total_liabilities_and_equity"])))

    def test_branch_filter_applies_to_rollup(self):
        from apps.organizations.models import Branch
        b = Branch.objects.create(organization=self.bakery, name="North")
        post_journal(self.bakery, "2026-02-11", [Line(self.water, debit=D("9")), Line(self.cash, credit=D("9"))], "t", 99, "TB", "", b, self.alice)
        d = self.rows("profit-loss", branch=b.id)
        ex = {r["code"]: r for r in d["expenses"]}
        self.assertEqual(ex["6300"]["rollup_amount"], D("9"))
        self.assertEqual(D(str(d["total_expenses"])), D("9"))

    def test_date_window_and_isolation(self):
        d = self.rows("profit-loss", date_from="2026-03-01", date_to="2026-03-31")
        self.assertEqual((d["expenses"], d["income"]), ([], []))
        other = self.as_user(self.bob, self.other).get("/api/v1/reports/profit-loss/").data
        self.assertEqual(other["expenses"], [])
