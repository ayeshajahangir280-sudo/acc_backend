from django.conf import settings
from django.db import models, transaction


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OrgOwned(TimeStamped):
    """Every business row belongs to exactly one organization (tenant boundary)."""

    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True


class DocStatus(models.TextChoices):
    POSTED = "posted", "Posted"
    VOID = "void", "Void"


class DocumentBase(OrgOwned):
    """Common fields of every posted accounting document (invoice, receipt, voucher...)."""

    number = models.CharField(max_length=32)
    date = models.DateField()
    branch = models.ForeignKey("organizations.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    narration = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.POSTED)
    journal = models.OneToOneField(
        "ledger.JournalTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Client-supplied key: resubmitting the same key returns the original document (no double posting).
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        abstract = True


class NumberSequence(models.Model):
    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=40)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "key"], name="uniq_sequence_per_org_key")]


def next_number(organization, key, prefix):
    """Gap-free per-organization numbering. Must be called inside transaction.atomic()."""
    seq, _ = NumberSequence.objects.get_or_create(organization=organization, key=key)
    seq = NumberSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_value += 1
    seq.save(update_fields=["last_value"])
    return f"{prefix}-{seq.last_value:06d}"


class ApprovalStatus(models.TextChoices):
    POSTED = "posted", "Posted"
    VOID = "void", "Void"
    PENDING = "pending", "Pending approval"
    REJECTED = "rejected", "Rejected"


class ApprovalDocumentBase(DocumentBase):
    """A document that may wait for approval before it posts. While pending/rejected it has no journal, no stock movement
    and is invisible to every ledger-based report; only `posted` documents are payable or reportable."""

    status = models.CharField(max_length=10, choices=ApprovalStatus.choices, default=ApprovalStatus.POSTED)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        abstract = True
