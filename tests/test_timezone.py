import datetime
import os
import subprocess
import sys

from django.conf import settings
from django.utils import timezone

from apps.ledger.models import JournalLine, JournalTransaction

from .base import AccountingTestCase


class TimezoneTests(AccountingTestCase):
    def test_configured_for_dubai_with_tz_support(self):
        self.assertEqual(settings.TIME_ZONE, "Asia/Dubai")
        self.assertTrue(settings.USE_TZ)

    def test_aware_timestamps_convert_to_dubai(self):
        utc = datetime.datetime(2026, 10, 31, 21, 30, tzinfo=datetime.timezone.utc)
        local = timezone.localtime(utc)
        self.assertEqual((local.date().isoformat(), local.hour, local.minute), ("2026-11-01", 1, 30))
        self.assertEqual(local.utcoffset(), datetime.timedelta(hours=4))

    def test_plain_document_dates_are_not_shifted(self):
        # Dates posted close to midnight / year-end must come back exactly as sent, everywhere.
        for d in ("2026-12-31", "2026-01-01", "2026-03-31"):
            r = self.sale(date=d)
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data["date"], d)
            self.assertEqual(self.client.get(f"/api/v1/sales/invoices/{r.data['id']}/").data["date"], d)
            txn = JournalTransaction.objects.get(source_id=r.data["id"], source_type="sales.invoice")
            self.assertEqual(txn.date.isoformat(), d)
            self.assertEqual({l.date.isoformat() for l in JournalLine.objects.filter(transaction=txn)}, {d})

    def test_timezone_is_hardcoded_not_environment_driven(self):
        env = {**os.environ, "DJANGO_TIME_ZONE": "UTC", "DJANGO_DEBUG": "true", "DATABASE_URL": "",
               "DJANGO_SETTINGS_MODULE": "config.settings"}
        out = subprocess.run([sys.executable, "-c", "from django.conf import settings; print(settings.TIME_ZONE)"],
                             cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "Asia/Dubai", out.stderr)
