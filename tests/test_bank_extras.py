"""OFX/QIF import, bank rules, create-from-statement-line and the reconciliation report export."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection

from apps.banking import formats
from apps.banking.models import BankStatement, StatementLine, StatementMatch
from apps.core.errors import DomainError
from apps.ledger.models import Account, JournalLine
from apps.organizations.models import Membership

from .base import D
from .test_banking import BASE, BankBase

User = get_user_model()
RULES = "/api/v1/bank-rules/"

OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260302120000<TRNAMT>150.00<FITID>A1<NAME>ACME LTD<MEMO>Invoice 7</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260305<TRNAMT>-42.50<FITID>A2<NAME>SHELL FUEL</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""
QIF = b"!Type:Bank\nD03/02/2026\nT150.00\nPACME LTD\nMInvoice 7\nN101\n^\nD03/05/2026\nT-42.50\nPSHELL FUEL\n^\n"


class FormatTests(BankBase):
    def test_parse_ofx_and_qif_rows(self):
        rows, errors = formats.parse_ofx(OFX)
        self.assertEqual(errors, [])
        self.assertEqual([(str(r["date"]), r["amount"], r["reference"]) for r in rows], [("2026-03-02", D("150.00"), "A1"), ("2026-03-05", D("-42.50"), "A2")])
        self.assertEqual(rows[0]["description"], "ACME LTD Invoice 7")
        rows, errors = formats.parse_qif(QIF, "mdy")
        self.assertEqual([(str(r["date"]), r["amount"], r["reference"]) for r in rows], [("2026-03-02", D("150.00"), "101"), ("2026-03-05", D("-42.50"), "")])

    def test_bad_rows_reported_with_row_numbers(self):
        bad = OFX.replace(b"20260305", b"2026xx05")
        rows, errors = formats.parse_ofx(bad)
        self.assertEqual(errors[0]["row"], 2)
        with self.assertRaises(DomainError):
            formats.parse_ofx(b"<OFX></OFX>")

    def test_import_ofx_and_qif_through_api_and_duplicate_protection(self):
        r = self.upload(OFX, opening="0", closing="107.50", name="march.ofx")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(StatementLine.objects.count(), 2)
        again = self.upload(OFX, opening="0", closing="107.50", name="march.ofx")
        self.assertEqual(again.data["code"], "duplicate_statement")
        # the same movements arriving as a QIF are recognised as already imported (same fingerprints are NOT expected: different
        # descriptions), so a QIF of the same period imports as a separate statement with its own lines
        q = self.upload(QIF, opening="0", closing="107.50", name="march.qif", date_format="mdy")
        self.assertEqual(q.status_code, 201, q.content)

    def test_bad_footing_and_invalid_rows_import_nothing(self):
        r = self.upload(OFX, opening="0", closing="1.00", name="x.ofx")
        self.assertEqual(r.data["code"], "statement_not_footing")
        r = self.upload(OFX.replace(b"-42.50", b"abc"), opening="0", closing="1.00", name="y.ofx")
        self.assertEqual(r.data["code"], "invalid_rows")
        self.assertEqual(StatementLine.objects.count(), 0)

    def test_csv_import_still_works(self):
        r = self.statement([("2026-03-02", "dep", "", "10.00")])
        self.assertEqual(r["summary"]["line_count"], 1)


class RuleAndCreateTests(BankBase):
    def make_statement(self):
        raw = (b"Date,Description,Reference,Amount\n2026-03-02,ACME LTD sale,R1,150.00\n2026-03-05,SHELL FUEL STATION,R2,-42.50\n"
               b"2026-03-06,Mystery,R3,-5.00\n")
        r = self.upload(raw, opening="0", closing="102.50")
        self.assertEqual(r.status_code, 201, r.content)
        return BankStatement.objects.get()

    def setUp(self):
        super().setUp()
        self.fuel = Account.objects.create(organization=self.bakery, code="6190", name="Fuel", type="expense")
        self.sales = Account.objects.create(organization=self.bakery, code="4190", name="Other income", type="income")

    def rule(self, **kw):
        body = {"name": "Fuel", "contains": "shell", "direction": "out", "category": self.fuel.id, "tax_rate": "5", **kw}
        r = self.client.post(RULES, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def test_rule_validation_and_suggestions(self):
        for patch, code in (({"contains": "ab"}, "rule_too_broad"), ({"category": self.bank.id}, "invalid_category"),
                            ({"category": self.acct("ACCOUNTS_RECEIVABLE").id}, "invalid_category"), ({"tax_rate": "200"}, "invalid_tax_rate")):
            body = {"name": "x", "contains": "shell", "category": self.fuel.id, **patch}
            self.assertEqual(self.client.post(RULES, body, format="json").data["code"], code, patch)
        self.rule()
        st = self.make_statement()
        sug = {s["description"]: s for s in self.client.get(f"{BASE}{st.id}/suggestions/").data}
        self.assertEqual(sug["SHELL FUEL STATION"]["category"], self.fuel.id)
        self.assertIsNone(sug["Mystery"]["rule"])
        self.assertIsNone(sug["ACME LTD sale"]["rule"])           # the rule is for money out only

    def test_create_expense_from_line_posts_and_matches_atomically(self):
        st = self.make_statement()
        line = StatementLine.objects.get(description__startswith="SHELL")
        r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id, "tax_rate": "5"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.balance("TAX_INPUT"), D("2.02"))                       # 42.50 includes 5% VAT: 42.50 * 5/105
        jl = JournalLine.objects.get(account=self.fuel)
        self.assertEqual(jl.debit, D("40.48"))
        self.assertEqual(StatementMatch.objects.get().amount, D("42.50"))
        sums = self.client.get(f"{BASE}{st.id}/").data["summary"]
        self.assertEqual(sums["matched_lines"], 1)
        self.assertTrue(self.trial_balance_ok())
        again = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id}, format="json")
        self.assertEqual(again.data["code"], "line_fully_matched")
        self.assertEqual(JournalLine.objects.filter(account=self.fuel).count(), 1)

    def test_create_receipt_from_deposit_and_no_party_account_misuse(self):
        st = self.make_statement()
        line = StatementLine.objects.get(description__startswith="ACME")
        r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.sales.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.balance("BANK"), D("150.00"))
        for bad in (self.bank.id, self.acct("ACCOUNTS_PAYABLE").id):
            l2 = StatementLine.objects.get(description="Mystery")
            r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": l2.id, "category": bad}, format="json")
            self.assertEqual(r.data["code"], "invalid_category")

    def test_period_lock_and_reconciled_statement_roll_everything_back(self):
        st = self.make_statement()
        line = StatementLine.objects.get(description__startswith="SHELL")
        from datetime import date
        self.bakery.books_locked_through = date(2026, 3, 31)
        self.bakery.save(update_fields=["books_locked_through"])
        r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id}, format="json")
        self.assertEqual(r.data["code"], "period_locked")
        self.assertEqual((JournalLine.objects.filter(account=self.fuel).count(), StatementMatch.objects.count()), (0, 0))
        self.bakery.books_locked_through = None
        self.bakery.save(update_fields=["books_locked_through"])
        BankStatement.objects.update(status="reconciled")
        r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id}, format="json")
        self.assertEqual(r.data["code"], "statement_reconciled")

    def test_voided_document_line_cannot_silently_reuse_old_document(self):
        st = self.make_statement()
        line = StatementLine.objects.get(description__startswith="SHELL")
        self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id}, format="json")
        from apps.expenses.models import Expense
        self.client.post(f"/api/v1/expenses/{Expense.objects.get().id}/void/")
        r = self.client.post(f"{BASE}{st.id}/create-from-line/", {"line": line.id, "category": self.fuel.id}, format="json")
        self.assertEqual(r.status_code, 400)       # stale match still blocks (unmatch first), nothing duplicated
        self.assertEqual(Expense.objects.count(), 1)

    def test_permissions_isolation_and_report(self):
        st = self.make_statement()
        v = User.objects.create_user("vv", password="pw-12345-xx")
        Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        cv = self.as_user(v, self.bakery)
        self.assertEqual(cv.get(RULES).status_code, 403)
        self.assertEqual(self.as_user(self.bob, self.other).get(f"{BASE}{st.id}/report/").status_code, 404)
        self.assertEqual(self.as_user(self.bob, self.other).get(RULES).data, [])
        r = self.client.get(f"{BASE}{st.id}/report/")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode()
        self.assertIn("Opening balance", text)
        self.assertIn("SHELL FUEL STATION", text)
        p = self.client.get(f"{BASE}{st.id}/report/", {"fmt": "pdf"})
        self.assertTrue(p.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get(f"{BASE}{st.id}/report/", {"fmt": "x"}).data["code"], "invalid_format")

    def test_report_neutralises_spreadsheet_formulas(self):
        raw = b"Date,Description,Reference,Amount\n2026-03-02,=HYPERLINK(\"http://x\"),R1,10.00\n"
        self.assertEqual(self.upload(raw, opening="0", closing="10.00").status_code, 201)
        st = BankStatement.objects.get()
        text = self.client.get(f"{BASE}{st.id}/report/").content.decode()
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)
