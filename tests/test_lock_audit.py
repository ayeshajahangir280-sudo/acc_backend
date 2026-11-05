"""Audit trail for period-lock changes and fiscal-year reopening."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model

from apps.organizations.models import FiscalYearClose, Membership, Organization, PeriodLockChange

from .base import D
from .test_opening_close import OpeningBase

User = get_user_model()


class LockAuditTests(OpeningBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.sale(qty="1", price="1000", tax="0", date="2026-03-10").status_code, 201)
        self.assertEqual(self.sale(qty="1", price="200", tax="0", date="2027-01-20").status_code, 201)

    def org_url(self, tail=""):
        return f"/api/v1/organizations/{self.bakery.id}/{tail}"

    def lock(self):
        return Organization.objects.get(pk=self.bakery.pk).books_locked_through

    def admin_client(self):
        u = User.objects.create_user("adm", password="pw-12345-xx")
        Membership.objects.create(user=u, organization=self.bakery, role="admin")
        return self.as_user(u, self.bakery)

    def test_manual_changes_are_recorded_and_lowering_needs_a_reason(self):
        r = self.client.patch(self.org_url(), {"books_locked_through": "2026-01-31"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.patch(self.org_url(), {"books_locked_through": "2026-02-28"}, format="json")
        self.assertEqual(r.status_code, 200)
        r = self.client.patch(self.org_url(), {"books_locked_through": "2026-01-15"}, format="json")
        self.assertEqual(r.data["code"], "reason_required")
        self.assertEqual(self.lock(), date(2026, 2, 28))
        r = self.client.patch(self.org_url(), {"books_locked_through": "2026-01-15", "lock_change_reason": "Late supplier invoice"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        rows = list(PeriodLockChange.objects.order_by("id").values_list("old_value", "new_value", "source", "reason", "changed_by__username"))
        self.assertEqual(rows, [(None, date(2026, 1, 31), "manual", "", "alice"), (date(2026, 1, 31), date(2026, 2, 28), "manual", "", "alice"),
                                (date(2026, 2, 28), date(2026, 1, 15), "manual", "Late supplier invoice", "alice")])
        # an unrelated edit records nothing
        self.client.patch(self.org_url(), {"legal_name": "Bakery Ltd"}, format="json")
        self.assertEqual(PeriodLockChange.objects.count(), 3)

    def test_clearing_the_lock_needs_a_reason_too(self):
        self.client.patch(self.org_url(), {"books_locked_through": "2026-01-31"}, format="json")
        self.assertEqual(self.client.patch(self.org_url(), {"books_locked_through": None}, format="json").data["code"], "reason_required")
        r = self.client.patch(self.org_url(), {"books_locked_through": None, "lock_change_reason": "Correcting January"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(self.lock())

    def test_fiscal_close_is_recorded_and_cannot_be_undone_by_editing_the_lock(self):
        with patch("django.utils.timezone.localdate", lambda *a, **k: date(2027, 6, 1)):
            self.assertEqual(self.client.post(self.org_url("close-year/"), {"year_end": "2026-12-31"}, format="json").status_code, 201)
            c = PeriodLockChange.objects.get(source="fiscal_close")
            self.assertEqual((c.old_value, c.new_value, c.changed_by.username), (None, date(2026, 12, 31), "alice"))
            for to in ("2026-06-30", None):
                r = self.client.patch(self.org_url(), {"books_locked_through": to, "lock_change_reason": "trying to sidestep"}, format="json")
                self.assertEqual(r.data["code"], "fiscal_year_closed")
            self.assertEqual(self.lock(), date(2026, 12, 31))
            # moving forward past the close is fine
            self.assertEqual(self.client.patch(self.org_url(), {"books_locked_through": "2027-01-31"}, format="json").status_code, 200)

    def test_reopen_restores_previous_lock_keeps_record_and_allows_reclose(self):
        with patch("django.utils.timezone.localdate", lambda *a, **k: date(2027, 6, 1)):
            self.client.post(self.org_url("close-year/"), {"year_end": "2026-12-31"}, format="json")
            r = self.client.post(self.org_url("reopen-year/"), {"year_end": "2026-12-31", "reason": "Auditor adjustment"}, format="json")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertIsNotNone(r.data["reopened_at"])
            self.assertIsNone(self.lock())
            self.assertEqual(FiscalYearClose.objects.count(), 1)
            ch = PeriodLockChange.objects.get(source="fiscal_reopen")
            self.assertEqual((ch.old_value, ch.new_value, ch.reason, ch.changed_by.username), (date(2026, 12, 31), None, "Auditor adjustment", "alice"))
            self.assertEqual(self.sale(date="2026-12-31").status_code, 201)          # the year accepts postings again
            again = self.client.post(self.org_url("close-year/"), {"year_end": "2026-12-31"}, format="json")
            self.assertEqual(again.status_code, 201, again.content)                    # and can be closed again
            self.assertEqual(FiscalYearClose.objects.count(), 2)

    def test_reopen_controls(self):
        with patch("django.utils.timezone.localdate", lambda *a, **k: date(2028, 2, 10)):
            self.client.post(self.org_url("close-year/"), {"year_end": "2026-12-31"}, format="json")
            self.client.post(self.org_url("close-year/"), {"year_end": "2027-12-31"}, format="json")
            go = lambda c, ye, reason="because": c.post(self.org_url("reopen-year/"), {"year_end": ye, "reason": reason}, format="json")
            self.assertEqual(self.admin_client().post(self.org_url("reopen-year/"), {"year_end": "2027-12-31", "reason": "x y z"}, format="json").status_code, 403)
            self.assertEqual(go(self.client, "2027-12-31", "").data["code"], "reason_required")
            self.assertEqual(go(self.client, "2026-12-31").data["code"], "reopen_in_order")
            self.assertEqual(go(self.client, "2027-12-31").status_code, 200)
            self.assertEqual(self.lock(), date(2026, 12, 31))
            self.assertEqual(go(self.client, "2027-12-31").data["code"], "already_reopened")
            self.assertEqual(go(self.client, "2020-12-31").data["code"], "year_not_closed")
            # lock moved by hand after the close: reopen refuses rather than guess
            self.client.patch(self.org_url(), {"books_locked_through": "2027-02-01"}, format="json")
            self.assertEqual(go(self.client, "2026-12-31").data["code"], "lock_changed")

    def test_history_endpoint_roles_and_isolation(self):
        self.client.patch(self.org_url(), {"books_locked_through": "2026-01-31"}, format="json")
        r = self.client.get(self.org_url("lock-history/"))
        self.assertEqual((r.status_code, len(r.data), r.data[0]["by"]), (200, 1, "alice"))
        self.assertEqual(self.admin_client().get(self.org_url("lock-history/")).status_code, 200)
        v = User.objects.create_user("vw", password="pw-12345-xx")
        Membership.objects.create(user=v, organization=self.bakery, role="viewer")
        self.assertEqual(self.as_user(v, self.bakery).get(self.org_url("lock-history/")).status_code, 403)
        self.assertEqual(self.as_user(self.bob, self.other).get(self.org_url("lock-history/")).status_code, 404)
