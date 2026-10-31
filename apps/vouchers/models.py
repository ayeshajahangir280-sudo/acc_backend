from django.db import models
from django.db.models import Q

from apps.core.models import DocumentBase, OrgOwned


class Voucher(DocumentBase):
    class Type(models.TextChoices):
        GENERAL = "general", "General Voucher"
        JOURNAL = "journal", "Journal Voucher"
        CONTRA = "contra", "Contra Voucher"

    type = models.CharField(max_length=10, choices=Type.choices)
    total = models.DecimalField(max_digits=18, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_voucher_number"),
            models.UniqueConstraint(fields=["organization", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="uniq_voucher_idem"),
        ]
        indexes = [models.Index(fields=["organization", "type", "-date", "-id"])]


class VoucherLine(OrgOwned):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey("ledger.Account", on_delete=models.PROTECT, related_name="+")
    party = models.ForeignKey("parties.Party", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    debit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    description = models.CharField(max_length=300, blank=True)
