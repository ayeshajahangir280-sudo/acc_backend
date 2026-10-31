from decimal import Decimal

from apps.organizations.models import Membership

from .base import AccountingTestCase, D


class ReportBase(AccountingTestCase):
    """Scenario (all amounts in a single org):
    SI-1  2026-01-10  100 + 10% tax = 110   (due 2026-01-10)
    PI-1  2026-01-12  200 + 5% tax  = 210   (due 2026-01-12)
    RC-1  2026-02-01  receipt 50   applied to SI-1
    PV-1  2026-02-05  payment 100  applied to PI-1
    SR-1  2026-02-10  return 1 x 50 + tax = 55 against SI-1
    """

    def setUp(self):
        super().setUp()
        self.si = self.sale(date="2026-01-10").data  # 2 x 50, 10%
        self.pi = self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-12", "items": [
            {"description": "Flour", "quantity": "10", "unit_price": "20", "tax_rate": "5"}]}, format="json").data
        self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("BANK").id, "amount": "50",
                         "date": "2026-02-01", "allocations": [{"invoice": self.si["id"], "amount": "50"}]}, format="json")
        self.client.post("/api/v1/payments/", {"party": self.supplier.id, "cash_account": self.acct("CASH").id, "amount": "100",
                         "date": "2026-02-05", "allocations": [{"invoice": self.pi["id"], "amount": "100"}]}, format="json")
        self.client.post("/api/v1/sales/invoices/", {"kind": "return", "original": self.si["id"], "party": self.customer.id,
                         "date": "2026-02-10", "items": [{"description": "Bread", "quantity": "1", "unit_price": "50", "tax_rate": "10"}]}, format="json")

    def get(self, url, client=None):
        return (client or self.client).get(f"/api/v1/reports/{url}")


class TrialBalanceTests(ReportBase):
    def test_balanced_and_matches_ledger(self):
        r = self.get("trial-balance/").data
        self.assertTrue(r["balanced"])
        self.assertEqual(D(r["total_debit"]), D(r["total_credit"]))
        by = {x["code"]: x for x in r["rows"]}
        self.assertEqual(D(by["1200"]["debit"]), D("5.00"))      # AR: 110 - 50 receipt - 55 return
        self.assertEqual(D(by["2000"]["credit"]), D("110.00"))   # AP: 210 - 100
        self.assertEqual(D(by["1100"]["debit"]), D("50.00"))
        self.assertEqual(D(by["1000"]["credit"]), D("100.00"))   # cash paid out -> negative cash

    def test_as_of_filter(self):
        r = self.get("trial-balance/?as_of=2026-01-11").data
        by = {x["code"]: x for x in r["rows"]}
        self.assertEqual(D(by["1200"]["debit"]), D("110.00"))
        self.assertNotIn("2000", by)  # purchase not yet dated
        self.assertTrue(r["balanced"])


class ProfitLossTests(ReportBase):
    def test_figures_equal_ledger(self):
        r = self.get("profit-loss/").data
        self.assertEqual(D(r["total_income"]), D("50.00"))      # 100 sales - 50 returns
        self.assertEqual(D(r["total_expenses"]), D("200.00"))
        self.assertEqual(D(r["net_profit"]), D("-150.00"))

    def test_date_range(self):
        r = self.get("profit-loss/?date_from=2026-01-01&date_to=2026-01-11").data
        self.assertEqual((D(r["total_income"]), D(r["total_expenses"]), D(r["net_profit"])), (D("100.00"), D("0"), D("100.00")))
        r = self.get("profit-loss/?date_from=2026-02-01&date_to=2026-02-28").data
        self.assertEqual(D(r["total_income"]), D("-50.00"))  # only the return fell in February

    def test_invalid_range(self):
        self.assertEqual(self.get("profit-loss/?date_from=2026-03-01&date_to=2026-01-01").status_code, 400)
        self.assertEqual(self.get("profit-loss/?date_from=nope").status_code, 400)


class BalanceSheetTests(ReportBase):
    def test_balances_at_every_date(self):
        for as_of in ("", "2026-01-11", "2026-01-31", "2026-02-03", "2026-02-28"):
            r = self.get(f"balance-sheet/?as_of={as_of}").data
            self.assertTrue(r["balanced"], as_of)
            self.assertEqual(D(r["total_assets"]), D(r["total_liabilities_and_equity"]))

    def test_current_values(self):
        r = self.get("balance-sheet/").data
        self.assertEqual(D(r["current_period_earnings"]), D("-150.00"))
        self.assertEqual(D(r["total_assets"]), D("5.00") + D("50.00") - D("100.00") + D("10.00"))  # AR + bank - cash + input tax
        self.assertEqual(D(r["total_liabilities"]), D("110.00") + D("5.00"))  # AP + output tax payable (10 - 5 return)


class AgingTests(ReportBase):
    def rows(self, q):
        d = self.get(f"aging/?{q}").data
        return d, {r["party_name"]: r for r in d["rows"]}

    def test_buckets_by_days_past_due(self):
        # SI-1 outstanding at 2026-02-15 = 110 - 50 receipt - 55 return = 5, due 2026-01-10 (36 days late)
        d, rows = self.rows("type=receivable&as_of=2026-02-15")
        self.assertEqual(D(rows["QA".replace("QA", "Cust A")]["buckets"]["31-60"]), D("5.00"))
        self.assertEqual(d["labels"], ["current", "1-30", "31-60", "61-90", "90+"])

    def test_current_and_each_bucket(self):
        cases = {"2026-01-10": "current", "2026-01-25": "1-30", "2026-03-01": "31-60"}
        for as_of, bucket in cases.items():
            _, rows = self.rows(f"type=receivable&as_of={as_of}")
            r = rows["Cust A"]
            self.assertEqual(D(r["buckets"][bucket]), D(r["outstanding"]), (as_of, bucket))
            self.assertNotEqual(D(r["outstanding"]), 0)
        _, rows = self.rows("type=receivable&as_of=2026-06-01")
        self.assertEqual(D(rows["Cust A"]["buckets"]["90+"]), D("5.00"))

    def test_partial_payment_is_reflected_per_invoice(self):
        _, rows = self.rows("type=payable&as_of=2026-02-20")  # PI-1: 210 - 100 = 110, 39 days late
        self.assertEqual(D(rows["Supp A"]["buckets"]["31-60"]), D("110.00"))

    def test_unapplied_receipt_reconciles_to_ledger(self):
        self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("CASH").id,
                         "amount": "20", "date": "2026-02-12"}, format="json")
        d, rows = self.rows("type=receivable&as_of=2026-02-15")
        r = rows["Cust A"]
        self.assertEqual(D(r["unapplied"]), D("-20.00"))
        self.assertEqual(D(r["outstanding"]) + D(r["unapplied"]), D(r["ledger_balance"]))
        ar = self.balance("ACCOUNTS_RECEIVABLE")
        self.assertEqual(D(d["totals"]["ledger_balance"]), ar)

    def test_custom_buckets_and_validation(self):
        d, _ = self.rows("type=receivable&as_of=2026-02-15&buckets=15,45")
        self.assertEqual(d["labels"], ["current", "1-15", "16-45", "45+"])
        for bad in ("buckets=a,b", "buckets=60,30", "buckets=0,10", "buckets=1,2,3,4,5,6", "as_of=garbage", "type=other"):
            self.assertEqual(self.get(f"aging/?{bad}").status_code, 400, bad)

    def test_party_filter(self):
        d = self.get(f"aging/?type=receivable&as_of=2026-02-15&party={self.supplier.id}").data
        self.assertEqual(d["rows"], [])


class VendorReportTests(ReportBase):
    def test_movements_and_closing_match_ledger(self):
        r = self.get("vendor/?date_from=2026-02-01&date_to=2026-02-28").data
        row = r["rows"][0]
        self.assertEqual((D(row["opening"]), D(row["purchases"]), D(row["payments"]), D(row["closing"])),
                         (D("210.00"), D("0"), D("100.00"), D("110.00")))
        bal = self.client.get("/api/v1/party-balances/").data
        self.assertEqual(D(bal[1]["payable"] if bal[0]["name"] == "Cust A" else bal[0]["payable"]), D(row["closing"]))

    def test_whole_period(self):
        row = self.get("vendor/").data["rows"][0]
        self.assertEqual((D(row["purchases"]), D(row["payments"]), D(row["closing"])), (D("210.00"), D("100.00"), D("110.00")))

    def test_return_is_separated(self):
        self.client.post("/api/v1/purchases/invoices/", {"kind": "return", "original": self.pi["id"], "party": self.supplier.id,
                         "date": "2026-02-20", "items": [{"description": "x", "quantity": "1", "unit_price": "20", "tax_rate": "5"}]}, format="json")
        row = self.get("vendor/").data["rows"][0]
        self.assertEqual((D(row["returns"]), D(row["closing"])), (D("21.00"), D("89.00")))


class SalesSummaryTests(ReportBase):
    def test_monthly(self):
        rows = {r["label"]: r for r in self.get("sales-summary/?group_by=month").data["rows"]}
        self.assertEqual((D(rows["2026-01"]["sales"]), D(rows["2026-01"]["sales_tax"]), D(rows["2026-01"]["sales_total"])),
                         (D("100.00"), D("10.00"), D("110.00")))
        self.assertEqual((D(rows["2026-02"]["returns_total"]), D(rows["2026-02"]["net_total"])), (D("55.00"), D("-55.00")))

    def test_totals_and_customer_grouping(self):
        d = self.get("sales-summary/?group_by=customer").data
        self.assertEqual(d["rows"][0]["label"], "Cust A")
        self.assertEqual(D(d["totals"]["net_total"]), D("55.00"))
        self.assertEqual((d["totals"]["invoices"], d["totals"]["returns"]), (1, 1))

    def test_date_filter_and_daily(self):
        d = self.get("sales-summary/?group_by=day&date_from=2026-02-01&date_to=2026-02-28").data
        self.assertEqual([r["label"] for r in d["rows"]], ["2026-02-10"])

    def test_void_excluded(self):
        self.client.post(f"/api/v1/sales/invoices/{self.si['id']}/void/")  # blocked (has return) -> still counted
        self.assertEqual(D(self.get("sales-summary/").data["totals"]["sales_total"]), D("110.00"))
        lone = self.sale(date="2026-03-01", qty="1", price="10", tax="0").data
        self.client.post(f"/api/v1/sales/invoices/{lone['id']}/void/")
        self.assertEqual(D(self.get("sales-summary/").data["totals"]["sales_total"]), D("110.00"))

    def test_bad_params(self):
        self.assertEqual(self.get("sales-summary/?group_by=year").status_code, 400)


class ReportAccessTests(ReportBase):
    def test_other_org_sees_nothing(self):
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(self.get("trial-balance/", bob).data["rows"], [])
        self.assertEqual(self.get("sales-summary/", bob).data["rows"], [])
        self.assertEqual(self.get("aging/?as_of=2026-02-15", bob).data["rows"], [])
        self.assertEqual(self.get("vendor/", bob).data["rows"], [])

    def test_foreign_org_header_forbidden_and_anonymous_rejected(self):
        self.assertEqual(self.get("trial-balance/", self.as_user(self.bob, self.bakery)).status_code, 403)
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get("/api/v1/reports/trial-balance/").status_code, 401)

    def test_viewer_can_read_reports(self):
        Membership.objects.create(user=self.bob, organization=self.bakery, role="viewer")
        self.assertEqual(self.get("profit-loss/", self.as_user(self.bob, self.bakery)).status_code, 200)

    def test_report_query_counts_are_constant(self):
        with self.assertNumQueries(3):  # membership, accounts, grouped lines
            self.get("trial-balance/")


class VatAndTaxRateTests(ReportBase):
    def test_vat_summary_matches_documents(self):
        r = self.get("vat-summary/").data
        # output: 10 charged on SI-1, 5 reversed by the return. input: 10 on PI-1.
        self.assertEqual((D(r["output_tax"]["charged"]), D(r["output_tax"]["returned"]), D(r["output_tax"]["net"])),
                         (D("10.00"), D("5.00"), D("5.00")))
        self.assertEqual(D(r["input_tax"]["net"]), D("10.00"))
        self.assertEqual(D(r["net_payable"]), D("-5.00"))  # refundable position

    def test_vat_summary_date_range_and_validation(self):
        r = self.get("vat-summary/?date_from=2026-02-01&date_to=2026-02-28").data
        self.assertEqual((D(r["output_tax"]["net"]), D(r["input_tax"]["net"])), (D("-5.00"), D("0")))
        self.assertEqual(self.get("vat-summary/?date_from=bad").status_code, 400)

    def test_tax_rates_are_user_defined_unique_and_isolated(self):
        self.assertEqual(self.client.get("/api/v1/tax-rates/").data, [])  # nothing preloaded
        a = self.client.post("/api/v1/tax-rates/", {"name": "Standard", "rate": "5", "is_default": True}, format="json")
        self.assertEqual(a.status_code, 201, a.data)
        b = self.client.post("/api/v1/tax-rates/", {"name": "Zero", "rate": "0", "is_default": True}, format="json")
        self.assertEqual(b.status_code, 201)
        defaults = [r["name"] for r in self.client.get("/api/v1/tax-rates/").data if r["is_default"]]
        self.assertEqual(defaults, ["Zero"])  # only one default at a time
        self.assertEqual(self.client.post("/api/v1/tax-rates/", {"name": "Zero", "rate": "1"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/tax-rates/", {"name": "Bad", "rate": "150"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/tax-rates/", {"name": "Neg", "rate": "-1"}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/v1/tax-rates/{a.data['id']}/").status_code, 405)
        self.assertEqual(self.as_user(self.bob, self.other).get("/api/v1/tax-rates/").data, [])
        Membership.objects.create(user=self.bob, organization=self.bakery, role="viewer")
        viewer = self.as_user(self.bob, self.bakery)
        self.assertEqual(viewer.post("/api/v1/tax-rates/", {"name": "X", "rate": "1"}, format="json").status_code, 403)
