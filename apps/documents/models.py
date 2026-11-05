from django.conf import settings
from django.db import models

from apps.core.models import OrgOwned


class EmailLog(OrgOwned):
    """One row per attempt to email a document. Also the basis of the per-user hourly send limit."""

    doc_type = models.CharField(max_length=30)      # sales.invoice, purchases.invoice, party.statement
    doc_id = models.BigIntegerField()
    to = models.CharField(max_length=500)
    subject = models.CharField(max_length=300)
    status = models.CharField(max_length=10, choices=[("sent", "Sent"), ("failed", "Failed")])
    error = models.CharField(max_length=300, blank=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["organization", "doc_type", "doc_id"]), models.Index(fields=["sent_by", "created_at"])]
