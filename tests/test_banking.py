"""Bank reconciliation: CSV import, matching, duplicates, partial/incorrect amounts, voids, permissions, ledger integrity."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.banking import importer
from apps.banking.models import BankStatement, StatementLine, StatementMatch
from apps.ledger.models import Account, JournalLine, JournalTransaction
from apps.organizations.models import Branch, BranchAccess, Membership, Organization

from .base import AccountingTestCase, D

User = get_user_model()
BASE = "/api/v1/bank-statements/"
HEAD = "Date,Description,Reference,Amount\n"


def csv(*rows, head=HEAD):
    q = lambda c: f'"{c}"' if "," in str(c) else str(c)
    return (head + "\n".join(",".join(q(c) for c in r) for r in rows) + "\n").encode()


class BankBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.bank = self.acct("BANK")

    # ---- ledger activity on the bank account
    def receipt(self, amount, on="2026-03-02", client=None, **kw):
        r = (client or self.client).post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.bank.id, "amount": amount, "date": on, **kw}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def payment(self, amount, on="2026-03-03", **kw):
        r = self.client.post("/api/v1/payments/", {"party": self.supplier.id, "cash_account": self.bank.id, "amount": amount, "date": on, **kw}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def jl(self, doc):
        return JournalLine.objects.get(account=self.bank, transaction__reference=doc["number"], transaction__reversal_of__isnull=True)

    # ---- statements
    def upload(self, raw, opening="0", closing=None, client=None, account=None, name="stmt.csv", **extra):
        body = {"account": (account or self.bank).id, "opening_balance": opening, "closing_balance": closing, "file": SimpleUploadedFile(name, raw, content_type="text/csv"), **extra}
        return (client or self.client).post(BASE + "import/", body, format="multipart")

    def statement(self, rows, opening="0", closing=None, **kw):
        closing = closing if closing is not None else str(D(opening) + sum(D(str(r[3])) for r in rows))
        r = self.upload(csv(*rows), opening, closing, **kw)
        self.assertEqual(r.status_code, 201, r.content)
        return r.data

    def lines(self, st, client=None):
        return (client or self.client).get(f"{BASE}{st['id']}/lines/").data

    def match(self, st, line, jl, client=None, **kw):
        return (client or self.client).post(f"{BASE}{st['id']}/match/", {"line": line, "journal_line": jl, **kw}, format="json")

    def summary(self, st):
        return self.client.get(f"{BASE}{st['id']}/").data["summary"]

    def ledger_state(self):
        return (JournalTransaction.objects.count(), JournalLine.objects.count(),
                sorted(JournalLine.objects.values_list("id", "account_id", "debit", "credit")))


class ImportTests(BankBase):
    def test_amount_column_import_and_summary(self):
        st = self.statement([("2026-03-02", "Deposit", "A1", "100.00"), ("2026-03-03", "Cheque", "A2", "-40.50")], opening="1000")
        self.assertEqual((st["period_start"], st["period_end"], D(st["closing_balance"]), st["status"]), ("2026-03-02", "2026-03-03", D("1059.50"), "in_progress"))
        s = st["summary"]
        self.assertEqual((s["line_count"], s["unmatched_lines"], D(str(s["difference"]))), (2, 2, D("59.50")))
        self.assertFalse(s["can_complete"])
        self.assertEqual(self.lines(st)[1]["amount"], "-40.50")

    def test_debit_credit_columns_dmy_dates_thousands_and_brackets(self):
        raw = csv(("02/03/2026", "In", "", "", "1,250.00"), ("03/03/2026", "Out", "", "300.00", ""), head="Transaction Date,Details,Ref,Withdrawal,Deposit\n")
        r = self.upload(raw, "0", "950.00", date_format="dmy")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([x["amount"] for x in self.lines(r.data)], ["1250.00", "-300.00"])
        raw = csv(("2026-03-02", "Fee", "", "(12.50)"))
        self.assertEqual(self.upload(raw, "0", "-12.50", name="b.csv").status_code, 201)

    def test_must_foot(self):
        before = BankStatement.objects.count()
        r = self.upload(csv(("2026-03-02", "x", "", "100")), "0", "99")
        self.assertEqual((r.status_code, r.data["code"]), (400, "statement_not_footing"))
        self.assertEqual(r.data["computed_closing"], "100.00")
        self.assertEqual((BankStatement.objects.count(), StatementLine.objects.count()), (before, 0))

    def test_invalid_rows_import_nothing_and_name_the_rows(self):
        raw = csv(("2026-03-02", "ok", "", "10"), ("not-a-date", "x", "", "5"), ("2026-03-04", "x", "", "abc"), ("2026-03-05", "zero", "", "0"), ("2026-03-06", "", "", ""))
        r = self.upload(raw, "0", "10")
        self.assertEqual((r.status_code, r.data["code"], r.data["error_count"]), (400, "invalid_rows", 4))
        self.assertEqual([e["row"] for e in r.data["errors"]], [3, 4, 5, 6])
        self.assertEqual(StatementLine.objects.count(), 0)

    def test_file_level_validation(self):
        cases = {
            "no headers": (b"a,b\n1,2\n", "csv_headers"),
            "header only": (HEAD.encode(), "csv_empty"),
            "empty": (b"", "csv_empty"),
            "binary": (b"\xff\xfe\x00\x01garbage", "csv_encoding"),
        }
        for label, (raw, code) in cases.items():
            self.assertEqual(self.upload(raw, "0", "0").data["code"], code, label)
        self.assertEqual(self.upload(b"x" * (importer.MAX_BYTES + 1), "0", "0").data["code"], "file_too_large")
        with patch.object(importer, "MAX_ROWS", 2):
            self.assertEqual(self.upload(csv(*[("2026-03-02", "x", "", "1")] * 3), "0", "3").data["code"], "too_many_rows")
        self.assertEqual(self.upload(csv(("2026-03-02", "x", "", "1")), "0", "1", date_format="bogus").data["code"], "invalid_date_format")
        r = self.client.post(BASE + "import/", {"account": self.bank.id, "opening_balance": "0", "closing_balance": "0"}, format="multipart")
        self.assertEqual(r.data["code"], "file_missing")
        for bad in ("abc", "NaN", "Infinity"):
            self.assertEqual(self.upload(csv(("2026-03-02", "x", "", "1")), bad, "1").data["code"], "invalid_amount", bad)

    def test_account_must_be_an_active_bank_account_of_the_organization(self):
        raw = csv(("2026-03-02", "x", "", "1"))
        self.assertEqual(self.upload(raw, "0", "1", account=self.acct("ACCOUNTS_RECEIVABLE")).data["code"], "invalid_account")
        other_bank = Account.objects.get(organization=self.other, system_code="BANK")
        self.assertEqual(self.upload(raw, "0", "1", account=other_bank).data["code"], "invalid_account")
        Account.objects.filter(pk=self.bank.pk).update(is_active=False)
        self.assertEqual(self.upload(raw, "0", "1").data["code"], "invalid_account")

    def test_same_file_twice_is_rejected(self):
        raw = csv(("2026-03-02", "x", "", "10"))
        self.assertEqual(self.upload(raw, "0", "10").status_code, 201)
        r = self.upload(raw, "0", "10", name="again.csv")
        self.assertEqual((r.status_code, r.data["code"]), (400, "duplicate_statement"))
        self.assertEqual(BankStatement.objects.count(), 1)

    def test_identical_rows_in_one_file_are_kept(self):
        st = self.statement([("2026-03-02", "Coffee", "", "-3"), ("2026-03-02", "Coffee", "", "-3")])
        self.assertEqual(len(self.lines(st)), 2)

    def test_overlapping_statement_is_rejected_or_deduplicated(self):
        self.statement([("2026-03-02", "A", "", "10"), ("2026-03-03", "B", "", "20")])
        overlap = csv(("2026-03-03", "B", "", "20"), ("2026-03-04", "C", "", "5"))
        r = self.upload(overlap, "10", "35")
        self.assertEqual((r.status_code, r.data["code"], r.data["duplicate_lines"]), (400, "overlapping_statement", 1))
        r = self.upload(overlap, "30", "35", skip_duplicates="true")   # only C is new: 30 + 5
        self.assertEqual((r.status_code, r.data["skipped_duplicates"], len(self.lines(r.data))), (201, 1, 1))
        r = self.upload(overlap, "0", "0", skip_duplicates="true", name="x.csv")
        self.assertEqual(r.data["code"], "duplicate_statement")
        r = self.upload(csv(("2026-03-03", "B", "", "20")), "10", "30", skip_duplicates="true", name="y.csv")
        self.assertEqual(r.data["code"], "nothing_new")

    def test_other_accounts_do_not_collide(self):
        cash = self.acct("CASH")
        raw = csv(("2026-03-02", "A", "", "10"))
        self.assertEqual(self.upload(raw, "0", "10").status_code, 201)
        self.assertEqual(self.upload(raw, "0", "10", account=cash).status_code, 201)

    def test_opening_balance_warning_against_last_reconciled(self):
        r1 = self.receipt("10", "2026-03-02")
        st = self.statement([("2026-03-02", "A", "", "10")])
        self.match(st, self.lines(st)[0]["id"], self.jl(r1).id)
        self.client.post(f"{BASE}{st['id']}/complete/")
        r = self.upload(csv(("2026-04-02", "B", "", "5")), "99", "104", name="apr.csv")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.data["warnings"]), 1)

    def test_import_posts_nothing_to_the_ledger(self):
        before = self.ledger_state()
        self.statement([("2026-03-02", "A", "", "10")])
        self.assertEqual(self.ledger_state(), before)


class MatchingTests(BankBase):
    def test_exact_match_reconciles_without_touching_the_ledger(self):
        r1, p1 = self.receipt("100"), self.payment("40")
        st = self.statement([("2026-03-02", "Dep", "", "100"), ("2026-03-03", "Chq", "", "-40")])
        before = self.ledger_state()
        a, b = self.lines(st)
        self.assertEqual(self.match(st, a["id"], self.jl(r1).id).status_code, 201)
        self.assertEqual(self.summary(st)["unmatched_lines"], 1)
        self.assertEqual(self.match(st, b["id"], self.jl(p1).id).status_code, 201)
        s = self.summary(st)
        self.assertEqual((s["matched_lines"], D(str(s["difference"])), s["can_complete"]), (2, D("0"), True))
        done = self.client.post(f"{BASE}{st['id']}/complete/")
        self.assertEqual((done.status_code, done.data["status"]), (200, "reconciled"))
        self.assertEqual(self.ledger_state(), before)
        self.assertTrue(self.trial_balance_ok())
        self.assertEqual(self.balance("BANK"), D("60"))

    def test_cannot_complete_while_unmatched(self):
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        r = self.client.post(f"{BASE}{st['id']}/complete/")
        self.assertEqual((r.status_code, r.data["code"]), (400, "lines_unmatched"))

    def test_candidates_are_filtered_and_ranked(self):
        r1, r2, p1 = self.receipt("100"), self.receipt("55", "2026-03-20"), self.payment("100")
        cash_receipt = self.client.post("/api/v1/receipts/", {"party": self.customer.id, "cash_account": self.acct("CASH").id, "amount": "100", "date": "2026-03-02"}, format="json")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        got = self.client.get(f"{BASE}{st['id']}/candidates/", {"line": line, "days": 30}).data
        ids = [c["journal_line"] for c in got]
        self.assertEqual(ids[0], self.jl(r1).id)                  # exact amount first
        self.assertIn(self.jl(r2).id, ids)
        self.assertNotIn(self.jl(p1).id, ids)                     # wrong direction
        self.assertEqual(len(ids), 2)                             # the cash-account receipt is another account
        narrow = self.client.get(f"{BASE}{st['id']}/candidates/", {"line": line, "days": 5}).data
        self.assertEqual([c["journal_line"] for c in narrow], [self.jl(r1).id])
        self.assertEqual(self.client.get(f"{BASE}{st['id']}/candidates/", {"line": 99999}).data["code"], "line_not_found")
        self.assertEqual(self.client.get(f"{BASE}{st['id']}/candidates/", {"days": "x"}).data["code"], "invalid_number")
        everything = self.client.get(f"{BASE}{st['id']}/candidates/").data
        self.assertEqual({c["journal_line"] for c in everything}, {self.jl(r1).id, self.jl(r2).id, self.jl(p1).id})

    def test_wrong_direction_account_and_foreign_references(self):
        r1, p1 = self.receipt("100"), self.payment("100")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        self.assertEqual(self.match(st, line, self.jl(p1).id).data["code"], "direction_mismatch")
        cash_line = JournalLine.objects.get(account=self.acct("CASH"), transaction__source_type="treasury.receipt") if False else None
        ar_line = JournalLine.objects.filter(account=self.acct("ACCOUNTS_RECEIVABLE")).first()
        self.assertEqual(self.match(st, line, ar_line.id).data["code"], "wrong_account")
        foreign = JournalLine.objects.filter(organization=self.other).first() or self._foreign_line()
        self.assertEqual(self.match(st, line, foreign.id).data["code"], "ledger_line_not_found")
        self.assertEqual(self.match(st, 987654, self.jl(r1).id).data["code"], "line_not_found")
        self.assertEqual(StatementMatch.objects.count(), 0)

    def _foreign_line(self):
        bob = self.as_user(self.bob, self.other)
        party = self.other.id and __import__("apps.parties.models", fromlist=["Party"]).Party.objects.create(organization=self.other, name="C", kind="customer")
        bank = Account.objects.get(organization=self.other, system_code="BANK")
        bob.post("/api/v1/receipts/", {"party": party.id, "cash_account": bank.id, "amount": "100", "date": "2026-03-02"}, format="json")
        return JournalLine.objects.filter(organization=self.other, account=bank).first()

    def test_line_cannot_be_matched_twice(self):
        r1, r2 = self.receipt("100"), self.receipt("100")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        self.assertEqual(self.match(st, line, self.jl(r1).id).status_code, 201)
        self.assertEqual(self.match(st, line, self.jl(r1).id).data["code"], "already_matched")
        self.assertEqual(self.match(st, line, self.jl(r2).id).data["code"], "line_fully_matched")
        self.assertEqual(StatementMatch.objects.count(), 1)

    def test_ledger_entry_cannot_be_cleared_twice_even_across_statements(self):
        r1 = self.receipt("100")
        s1 = self.statement([("2026-03-02", "Dep", "", "100")])
        s2 = self.statement([("2026-03-03", "Dep again", "", "100")], name="two.csv") if False else self.statement([("2026-03-03", "Dep again", "", "100")])
        self.assertEqual(self.match(s1, self.lines(s1)[0]["id"], self.jl(r1).id).status_code, 201)
        r = self.match(s2, self.lines(s2)[0]["id"], self.jl(r1).id)
        self.assertEqual((r.status_code, r.data["code"]), (400, "ledger_already_cleared"))
        self.assertEqual(StatementMatch.objects.count(), 1)

    def test_one_ledger_entry_can_clear_several_statement_lines_up_to_its_amount(self):
        r1 = self.receipt("100")
        st = self.statement([("2026-03-02", "Part 1", "", "60"), ("2026-03-02", "Part 2", "", "40"), ("2026-03-03", "Extra", "", "5")])
        a, b, c = [x["id"] for x in self.lines(st)]
        self.assertEqual(self.match(st, a, self.jl(r1).id).data["amount"], "60.00")
        self.assertEqual(self.match(st, b, self.jl(r1).id).data["amount"], "40.00")
        self.assertEqual(self.match(st, c, self.jl(r1).id).data["code"], "ledger_already_cleared")

    def test_incorrect_amount_gives_partial_match_and_a_clear_difference(self):
        r1 = self.receipt("90")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        over = self.match(st, line, self.jl(r1).id, amount="95")
        self.assertEqual((over.status_code, over.data["code"]), (400, "over_match"))
        self.assertEqual(self.match(st, line, self.jl(r1).id, amount="0").data["code"], "invalid_amount")
        self.assertEqual(self.match(st, line, self.jl(r1).id, amount="-5").data["code"], "invalid_amount")
        ok = self.match(st, line, self.jl(r1).id)
        self.assertEqual(ok.data["amount"], "90.00")                       # default = what both sides can give
        row = self.lines(st)[0]
        self.assertEqual((row["status"], row["matched_amount"]), ("partial", D("90")))
        s = self.summary(st)
        self.assertEqual((s["partial_lines"], D(str(s["difference"])), s["can_complete"]), (1, D("10"), False))
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").data["code"], "lines_unmatched")
        # record the missing 10 in the books (an expense paid through the bank) and clear it: the books were never altered to fit the bank
        fuel = Account.objects.create(organization=self.bakery, code="6200", name="Bank fees", type="expense")
        e = self.client.post("/api/v1/expenses/", {"date": "2026-03-02", "expense_account": fuel.id, "paid_through": self.bank.id, "amount": "10", "tax_rate": "0"}, format="json")
        self.assertEqual(e.status_code, 201)
        # a fee is money out, so it cannot clear a deposit shortfall; fix the direction with a +10 receipt instead
        r2 = self.receipt("10")
        self.assertEqual(self.match(st, line, self.jl(r2).id).status_code, 201)
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").status_code, 200)

    def test_unmatch_returns_the_line_to_unmatched(self):
        r1 = self.receipt("100")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        m = self.match(st, line, self.jl(r1).id).data["id"]
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/unmatch/", {"match": m}, format="json").data["removed"], 1)
        self.assertEqual(self.lines(st)[0]["status"], "unmatched")
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/unmatch/", {"match": m}, format="json").data["code"], "match_not_found")
        self.assertEqual(self.match(st, line, self.jl(r1).id).status_code, 201)         # and it can be matched again
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/unmatch/", {"line": line}, format="json").data["removed"], 1)


class VoidedTransactionTests(BankBase):
    def test_voided_transactions_are_not_candidates_and_cannot_be_matched(self):
        r1 = self.receipt("100")
        jl_id = self.jl(r1).id
        self.client.post(f"/api/v1/receipts/{r1['id']}/void/")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        self.assertEqual(self.client.get(f"{BASE}{st['id']}/candidates/", {"line": line}).data, [])
        self.assertEqual(self.match(st, line, jl_id).data["code"], "transaction_void")
        mirror = JournalLine.objects.filter(account=self.bank, transaction__reversal_of__isnull=False).first()
        self.assertEqual(self.match(st, line, mirror.id).data["code"], "transaction_void")   # the reversal entry is void-status too

    def test_void_after_matching_makes_the_match_stale_and_blocks_completion(self):
        r1 = self.receipt("100")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        line = self.lines(st)[0]["id"]
        m = self.match(st, line, self.jl(r1).id).data["id"]
        self.assertEqual(self.client.post(f"/api/v1/receipts/{r1['id']}/void/").status_code, 200)    # allowed: not reconciled yet
        row = self.lines(st)[0]
        self.assertEqual((row["status"], row["matches"][0]["stale"]), ("unmatched", True))
        s = self.summary(st)
        self.assertEqual((s["stale_matches"], D(str(s["difference"])), s["can_complete"]), (1, D("100"), False))
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").data["code"], "stale_matches")
        self.client.post(f"{BASE}{st['id']}/unmatch/", {"match": m}, format="json")
        self.assertEqual(self.summary(st)["stale_matches"], 0)
        # a replacement receipt can now clear the line
        r2 = self.receipt("100")
        self.assertEqual(self.match(st, line, self.jl(r2).id).status_code, 201)
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").status_code, 200)

    def test_stale_match_does_not_hold_the_ledger_entry_or_line(self):
        r1 = self.receipt("100")
        s1 = self.statement([("2026-03-02", "Dep", "", "100")])
        self.match(s1, self.lines(s1)[0]["id"], self.jl(r1).id)
        self.client.post(f"/api/v1/receipts/{r1['id']}/void/")
        r2 = self.receipt("100")
        self.assertEqual(self.match(s1, self.lines(s1)[0]["id"], self.jl(r2).id).status_code, 201)   # line is free again

    def test_reconciled_transactions_cannot_be_voided_until_reopened(self):
        r1 = self.receipt("100")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        self.match(st, self.lines(st)[0]["id"], self.jl(r1).id)
        self.client.post(f"{BASE}{st['id']}/complete/")
        before = self.ledger_state()
        r = self.client.post(f"/api/v1/receipts/{r1['id']}/void/")
        self.assertEqual((r.status_code, r.data["code"]), (400, "transaction_reconciled"))
        self.assertEqual(self.ledger_state(), before)
        self.client.post(f"{BASE}{st['id']}/reopen/")
        self.assertEqual(self.client.post(f"/api/v1/receipts/{r1['id']}/void/").status_code, 200)


class AutoMatchTests(BankBase):
    def test_matches_only_unambiguous_exact_amounts_within_the_window(self):
        a, b = self.receipt("11.11", "2026-03-02"), self.receipt("22.22", "2026-03-05")
        far = self.receipt("33.33", "2026-04-20")
        d1, d2 = self.receipt("50", "2026-03-06"), self.receipt("50", "2026-03-06")     # two identical candidates
        st = self.statement([("2026-03-03", "A", "", "11.11"), ("2026-03-05", "B", "", "22.22"), ("2026-03-06", "far", "", "33.33"),
                             ("2026-03-06", "dup", "", "50"), ("2026-03-07", "none", "", "7")])
        r = self.client.post(f"{BASE}{st['id']}/auto-match/", {"days": 7}, format="json")
        self.assertEqual(r.data, {"matched": 2, "ambiguous": 1, "no_candidate": 2})
        status = {x["description"]: x["status"] for x in self.lines(st)}
        self.assertEqual((status["A"], status["B"], status["far"], status["dup"]), ("matched", "matched", "unmatched", "unmatched"))
        self.assertTrue(all(m.method == "auto" for m in StatementMatch.objects.all()))

    def test_reference_breaks_a_tie(self):
        d1, d2 = self.receipt("50", "2026-03-06"), self.receipt("50", "2026-03-06")
        st = self.statement([("2026-03-06", "Deposit", d2["number"], "50")])
        r = self.client.post(f"{BASE}{st['id']}/auto-match/", {}, format="json")
        self.assertEqual(r.data["matched"], 1)
        self.assertEqual(StatementMatch.objects.get().journal_line_id, self.jl(d2).id)

    def test_two_lines_competing_for_one_entry_are_left_for_a_human(self):
        self.receipt("50", "2026-03-06")
        st = self.statement([("2026-03-06", "one", "", "50"), ("2026-03-06", "two", "", "50")])
        r = self.client.post(f"{BASE}{st['id']}/auto-match/", {}, format="json")
        self.assertEqual((r.data["matched"], r.data["ambiguous"]), (0, 2))

    def test_auto_match_is_repeatable_and_validates_days(self):
        self.receipt("5", "2026-03-06")
        st = self.statement([("2026-03-06", "x", "", "5")])
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/auto-match/", {}, format="json").data["matched"], 1)
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/auto-match/", {}, format="json").data["matched"], 0)
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/auto-match/", {"days": 999}, format="json").data["code"], "invalid_number")


class LifecycleTests(BankBase):
    def reconciled(self, month="03", amount="10"):
        r = self.receipt(amount, f"2026-{month}-02")
        st = self.statement([(f"2026-{month}-02", "Dep", "", amount)]) if month == "03" else None
        return r, st

    def test_reconciled_statements_are_frozen(self):
        r1, r2 = self.receipt("10"), self.receipt("10")
        st = self.statement([("2026-03-02", "Dep", "", "10")])
        line = self.lines(st)[0]["id"]
        m = self.match(st, line, self.jl(r1).id).data["id"]
        self.client.post(f"{BASE}{st['id']}/complete/")
        self.assertEqual(self.match(st, line, self.jl(r2).id).data["code"], "statement_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/unmatch/", {"match": m}, format="json").data["code"], "statement_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/auto-match/", {}, format="json").data["code"], "statement_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/discard/").data["code"], "statement_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").data["code"], "statement_reconciled")
        self.assertEqual(self.client.get(f"{BASE}{st['id']}/").data["status"], "reconciled")

    def test_reopen_only_the_latest_and_then_edit(self):
        r1, r2 = self.receipt("10", "2026-03-02"), self.receipt("20", "2026-04-02")
        s1 = self.statement([("2026-03-02", "Dep", "", "10")])
        s2 = self.statement([("2026-04-02", "Dep", "", "20")], opening="10")
        self.match(s1, self.lines(s1)[0]["id"], self.jl(r1).id)
        self.match(s2, self.lines(s2)[0]["id"], self.jl(r2).id)
        self.client.post(f"{BASE}{s1['id']}/complete/")
        self.client.post(f"{BASE}{s2['id']}/complete/")
        self.assertEqual(self.client.post(f"{BASE}{s1['id']}/reopen/").data["code"], "later_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{s2['id']}/reopen/").status_code, 200)
        self.assertEqual(self.client.post(f"{BASE}{s2['id']}/reopen/").data["code"], "not_reconciled")
        self.assertEqual(self.client.post(f"{BASE}{s1['id']}/reopen/").status_code, 200)

    def test_discard_removes_an_unreconciled_import_and_allows_reimport(self):
        raw = csv(("2026-03-02", "x", "", "10"))
        st = self.upload(raw, "0", "10").data
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/discard/").status_code, 204)
        self.assertEqual((BankStatement.objects.count(), StatementLine.objects.count()), (0, 0))
        self.assertEqual(self.upload(raw, "0", "10").status_code, 201)

    def test_discard_clears_matches_but_not_the_ledger(self):
        r1 = self.receipt("10")
        st = self.statement([("2026-03-02", "Dep", "", "10")])
        self.match(st, self.lines(st)[0]["id"], self.jl(r1).id)
        before = self.ledger_state()
        self.client.post(f"{BASE}{st['id']}/discard/")
        self.assertEqual((StatementMatch.objects.count(), self.ledger_state()), (0, before))

    def test_works_inside_a_locked_period(self):
        r1 = self.receipt("10")
        st = self.statement([("2026-03-02", "Dep", "", "10")])
        Organization.objects.filter(pk=self.bakery.pk).update(books_locked_through="2026-12-31")
        self.assertEqual(self.match(st, self.lines(st)[0]["id"], self.jl(r1).id).status_code, 201)
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").status_code, 200)   # no ledger write, so the lock does not apply

    def test_reconciliation_totals_with_unbranched_and_branched_ledger_entries(self):
        north = Branch.objects.create(organization=self.bakery, name="North")
        r1, r2 = self.receipt("30"), self.receipt("70", branch=north.id)
        st = self.statement([("2026-03-02", "Both", "", "100")])
        line = self.lines(st)[0]["id"]
        self.match(st, line, self.jl(r1).id)
        self.match(st, line, self.jl(r2).id)
        s = self.summary(st)
        self.assertEqual((D(str(s["difference"])), D(str(s["book_balance_at_period_end"])), s["uncleared_book_items"]), (D("0"), D("100"), 0))
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").status_code, 200)

    def test_uncleared_book_items_are_reported(self):
        r1, r2 = self.receipt("100"), self.payment("30", "2026-03-02")
        st = self.statement([("2026-03-02", "Dep", "", "100")])
        self.match(st, self.lines(st)[0]["id"], self.jl(r1).id)
        s = self.summary(st)
        self.assertEqual((s["uncleared_book_items"], D(str(s["uncleared_book_net"])), D(str(s["book_balance_at_period_end"]))), (1, D("-30"), D("70")))
        self.assertEqual(self.client.post(f"{BASE}{st['id']}/complete/").status_code, 200)    # outstanding cheques do not block


class AccessTests(BankBase):
    def setUp(self):
        super().setUp()
        self.north = Branch.objects.create(organization=self.bakery, name="North")
        self.users = {}
        for name, role, branches in (("adm", "admin", []), ("acc", "accountant", [self.north]), ("acc0", "accountant", []), ("vw", "viewer", [self.north])):
            u = User.objects.create_user(name, password="pw-12345-xx")
            m = Membership.objects.create(user=u, organization=self.bakery, role=role)
            for b in branches:
                BranchAccess.objects.create(membership=m, branch=b)
            self.users[name] = self.as_user(u, self.bakery)
        r1 = self.receipt("100")
        self.st = self.statement([("2026-03-02", "Dep", "", "100")])
        self.r1 = r1

    def test_admin_can_use_it(self):
        adm = self.users["adm"]
        self.assertEqual(adm.get(BASE).data["count"], 1)
        self.assertEqual(self.match(self.st, self.lines(self.st)[0]["id"], self.jl(self.r1).id, client=adm).status_code, 201)

    def test_branch_restricted_users_and_viewers_have_no_access_at_all(self):
        line, jl = self.lines(self.st)[0]["id"], self.jl(self.r1).id
        for who in ("acc", "acc0", "vw"):
            c = self.users[who]
            sid = self.st["id"]
            codes = [c.get(BASE).status_code, c.get(f"{BASE}{sid}/").status_code, c.get(f"{BASE}{sid}/lines/").status_code,
                     c.get(f"{BASE}{sid}/candidates/").status_code, c.post(f"{BASE}{sid}/match/", {"line": line, "journal_line": jl}, format="json").status_code,
                     c.post(f"{BASE}{sid}/complete/").status_code, c.post(f"{BASE}{sid}/discard/").status_code,
                     self.upload(csv(("2026-05-02", "z", "", "1")), "0", "1", client=c, name=f"{who}.csv").status_code]
            self.assertEqual(set(codes), {403}, (who, codes))
        self.assertEqual(StatementMatch.objects.count(), 0)
        self.assertEqual(BankStatement.objects.count(), 1)

    def test_other_organizations_cannot_see_or_touch_statements(self):
        bob = self.as_user(self.bob, self.other)
        sid = self.st["id"]
        self.assertEqual(bob.get(BASE).data["count"], 0)
        for method, url in (("get", f"{BASE}{sid}/"), ("get", f"{BASE}{sid}/lines/"), ("post", f"{BASE}{sid}/complete/"), ("post", f"{BASE}{sid}/discard/"), ("post", f"{BASE}{sid}/auto-match/")):
            self.assertEqual(getattr(bob, method)(url).status_code, 404, url)
        self.assertEqual(self.as_user(self.bob, self.bakery).get(BASE).status_code, 403)       # not a member of the bakery

    def test_unauthenticated(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get(BASE).status_code, 401)


class ConstraintTests(BankBase):
    def test_database_blocks_double_clearing_pairs_and_bad_amounts(self):
        from django.db import IntegrityError, transaction

        r1 = self.receipt("10")
        st = self.statement([("2026-03-02", "Dep", "", "10")])
        line = StatementLine.objects.get(statement_id=st["id"])
        StatementMatch.objects.create(organization=self.bakery, line=line, journal_line=self.jl(r1), amount=D("10"))
        for kw in ({"amount": D("1")}, {"amount": D("-1")}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                StatementMatch.objects.create(organization=self.bakery, line=line, journal_line=self.jl(r1), **kw)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StatementLine.objects.create(organization=self.bakery, statement_id=st["id"], account=self.bank, row=99, date=date(2026, 3, 2), amount=D("0"), fingerprint="x")
        with self.assertRaises(IntegrityError), transaction.atomic():
            StatementLine.objects.create(organization=self.bakery, statement_id=st["id"], account=self.bank, row=98, date=date(2026, 3, 2), amount=D("5"), fingerprint=line.fingerprint)
