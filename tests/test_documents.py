"""PDF generation and email delivery of invoices, bills, credit notes and statements."""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings

from apps.documents.models import EmailLog
from apps.organizations.models import Branch, BranchAccess, Membership

from .base import AccountingTestCase, D

User = get_user_model()
SMTP = "django.core.mail.backends.smtp.EmailBackend"


class DocBase(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.customer.email = "cust@example.com"
        self.customer.address = "1 Baker St"
        self.customer.save()
        self.inv = self.sale(qty="2", price="50.00", tax="10").data
        self.bill = self.client.post("/api/v1/purchases/invoices/", {"party": self.supplier.id, "date": "2026-01-12", "items": [
            {"description": "Flour", "quantity": "10", "unit_price": "3", "tax_rate": "0"}]}, format="json").data

    def is_pdf(self, r):
        self.assertEqual(r.status_code, 200, getattr(r, "content", b"")[:200])
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertGreater(len(r.content), 800)


class PdfTests(DocBase):
    def test_invoice_bill_and_credit_note_pdfs(self):
        self.is_pdf(self.client.get(f"/api/v1/sales/invoices/{self.inv['id']}/pdf/"))
        self.is_pdf(self.client.get(f"/api/v1/purchases/invoices/{self.bill['id']}/pdf/"))
        ret = self.client.post("/api/v1/sales/invoices/", {"party": self.customer.id, "date": "2026-01-15", "kind": "return",
              "original": self.inv["id"], "items": [{"description": "Bread", "quantity": "1", "unit_price": "50", "tax_rate": "10"}]}, format="json")
        self.assertEqual(ret.status_code, 201, ret.content)
        r = self.client.get(f"/api/v1/sales/invoices/{ret.data['id']}/pdf/")
        self.is_pdf(r)
        self.assertIn("Credit-Note", r["Content-Disposition"])

    def test_void_document_still_renders(self):
        self.client.post(f"/api/v1/sales/invoices/{self.inv['id']}/void/")
        self.is_pdf(self.client.get(f"/api/v1/sales/invoices/{self.inv['id']}/pdf/"))

    def test_text_is_escaped_not_interpreted(self):
        r = self.sale(narration="<b>x</b> & <script>")
        self.assertEqual(r.status_code, 201)
        self.is_pdf(self.client.get(f"/api/v1/sales/invoices/{r.data['id']}/pdf/"))

    def test_isolation_branch_scope_and_viewer_read(self):
        self.assertEqual(self.as_user(self.bob, self.other).get(f"/api/v1/sales/invoices/{self.inv['id']}/pdf/").status_code, 404)
        n, s = (Branch.objects.create(organization=self.bakery, name=x) for x in "NS")
        r = self.sale(branch=s.id)
        nora = User.objects.create_user("nora", password="pw-12345-xx")
        m = Membership.objects.create(user=nora, organization=self.bakery, role=Membership.Role.ACCOUNTANT)
        BranchAccess.objects.create(membership=m, branch=n)
        cn = self.as_user(nora, self.bakery)
        self.assertEqual(cn.get(f"/api/v1/sales/invoices/{r.data['id']}/pdf/").status_code, 404)
        v = User.objects.create_user("vv", password="pw-12345-xx")
        mv = Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        BranchAccess.objects.create(membership=mv, branch=n)     # viewers are always branch-restricted
        mine = self.sale(branch=n.id).data
        self.is_pdf(self.as_user(v, self.bakery).get(f"/api/v1/sales/invoices/{mine['id']}/pdf/"))
        self.assertEqual(self.as_user(v, self.bakery).get(f"/api/v1/sales/invoices/{r.data['id']}/pdf/").status_code, 404)


class StatementTests(DocBase):
    def test_statement_matches_ledger_and_pdf(self):
        self.client.post("/api/v1/treasury/receipts/", {"party": self.customer.id, "date": "2026-01-20", "amount": "40.00",
                         "account": self.acct("CASH").id, "allocations": []}, format="json")
        d = self.client.get(f"/api/v1/parties/{self.customer.id}/statement/", {"date_to": "2026-12-31"}).data
        from apps.ledger.selectors import party_balances

        ledger = party_balances(self.bakery)[self.customer.id]["receivable"]
        self.assertEqual(D(str(d["closing"])), ledger)
        self.assertEqual(D(str(d["opening"])), D("0"))
        self.assertEqual(D(str(d["closing"])), D(str(d["rows"][-1]["balance"])))
        self.is_pdf(self.client.get(f"/api/v1/parties/{self.customer.id}/statement-pdf/"))

    def test_opening_balance_with_date_window(self):
        d = self.client.get(f"/api/v1/parties/{self.customer.id}/statement/", {"date_from": "2026-02-01", "date_to": "2026-02-28"}).data
        self.assertEqual(D(str(d["opening"])), D("110.00"))   # the 2 x 50 + 10% invoice is before the window
        self.assertEqual(d["rows"], [])

    def test_bad_inputs(self):
        u = f"/api/v1/parties/{self.customer.id}/statement/"
        self.assertEqual(self.client.get(u, {"date_from": "2026-03-01", "date_to": "2026-01-01"}).status_code, 400)
        self.assertEqual(self.client.get(u, {"side": "purchases"}).status_code, 400)   # customer has no payables side
        self.assertEqual(self.as_user(self.bob, self.other).get(u).status_code, 404)


class EmailTests(DocBase):
    def url(self):
        return f"/api/v1/sales/invoices/{self.inv['id']}/email/"

    def test_sends_pdf_attachment_to_party_email_and_logs(self):
        r = self.client.post(self.url(), {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual(m.to, ["cust@example.com"])
        self.assertEqual(m.attachments[0][2], "application/pdf")
        self.assertTrue(m.attachments[0][1].startswith(b"%PDF"))
        self.assertEqual(EmailLog.objects.get().status, "sent")

    def test_custom_recipients_and_validation(self):
        r = self.client.post(self.url(), {"to": "a@example.com; b@example.com", "subject": "Hi\nBcc: evil@x.com"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(mail.outbox[0].to, ["a@example.com", "b@example.com"])
        self.assertNotIn("\n", mail.outbox[0].subject)
        for bad, code in (("nope", "invalid_email"), ("a@x.com,b@x.com,c@x.com,d@x.com,e@x.com,f@x.com", "too_many_recipients")):
            r = self.client.post(self.url(), {"to": bad}, format="json")
            self.assertEqual(r.data["code"], code)
        self.assertEqual(len(mail.outbox), 1)

    def test_no_recipient(self):
        self.customer.email = ""
        self.customer.save()
        self.assertEqual(self.client.post(self.url(), {}, format="json").data["code"], "no_recipient")

    @override_settings(EMAIL_BACKEND=SMTP, EMAIL_HOST="")
    def test_not_configured_is_reported_not_faked(self):
        r = self.client.post(self.url(), {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "email_not_configured")
        self.assertEqual(EmailLog.objects.count(), 0)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="127.0.0.1", EMAIL_PORT=1, EMAIL_TIMEOUT=1)
    def test_smtp_failure_is_logged_and_reported(self):
        r = self.client.post(self.url(), {}, format="json")
        self.assertEqual(r.data["code"], "email_failed")
        self.assertEqual(EmailLog.objects.get().status, "failed")

    def test_void_viewer_and_other_org_blocked(self):
        v = User.objects.create_user("vv", password="pw-12345-xx")
        Membership.objects.create(user=v, organization=self.bakery, role=Membership.Role.VIEWER)
        self.assertEqual(self.as_user(v, self.bakery).post(self.url(), {}, format="json").status_code, 403)
        self.assertEqual(self.as_user(self.bob, self.other).post(self.url(), {}, format="json").status_code, 404)
        self.client.post(f"/api/v1/sales/invoices/{self.inv['id']}/void/")
        self.assertEqual(self.client.post(self.url(), {}, format="json").data["code"], "void_document")
        self.assertEqual(len(mail.outbox), 0)

    def test_hourly_limit(self):
        from apps.documents import mail as m

        for _ in range(m.HOURLY_LIMIT):
            EmailLog.objects.create(organization=self.bakery, doc_type="x", doc_id=1, to="a@b.co", subject="s", status="sent", sent_by=self.alice)
        self.assertEqual(self.client.post(self.url(), {}, format="json").data["code"], "email_rate_limited")

    def test_statement_email(self):
        r = self.client.post(f"/api/v1/parties/{self.customer.id}/statement-email/", {"date_to": "2026-12-31"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(mail.outbox[0].to, ["cust@example.com"])
        self.assertTrue(mail.outbox[0].attachments[0][1].startswith(b"%PDF"))
