from decimal import Decimal

from django.db import transaction

from apps.core.errors import DomainError
from apps.core.models import DocStatus, next_number
from apps.core.money import money
from apps.ledger.services import Line, post_journal, void_journal

from . import rules
from .models import Voucher, VoucherLine

PREFIX = {"general": "GV", "journal": "JV", "contra": "CV"}


@transaction.atomic
def create_voucher(org, user, vtype, *, date, lines, narration="", branch=None, idempotency_key=None):
    """lines: list of dicts {account, debit, credit, party?, description?}. Balance is enforced by the ledger."""
    if idempotency_key:
        existing = Voucher.objects.filter(organization=org, idempotency_key=idempotency_key).first()
        if existing:
            return existing, False
    if vtype not in PREFIX:
        raise DomainError("Unknown voucher type.", "invalid_type")
    rules.validate(vtype, lines)
    jl = [Line(l["account"], money(l.get("debit") or 0), money(l.get("credit") or 0), l.get("party"), l.get("description", ""))
          for l in lines]
    total = sum((l.debit for l in jl), Decimal("0"))
    voucher = Voucher.objects.create(
        organization=org, number=next_number(org, f"voucher.{vtype}", PREFIX[vtype]), date=date, branch=branch,
        narration=narration, type=vtype, total=total, idempotency_key=idempotency_key or None, created_by=user,
    )
    # post_journal validates: >=2 lines, one-sided lines, same-org accounts/parties, debit == credit
    voucher.journal = post_journal(org, date, jl, f"voucher.{vtype}", voucher.id, voucher.number, narration, branch, user)
    voucher.save(update_fields=["journal", "updated_at"])
    VoucherLine.objects.bulk_create(
        [VoucherLine(organization=org, voucher=voucher, account=l.account, party=l.party, debit=l.debit, credit=l.credit,
                     description=l.description) for l in jl]
    )
    return voucher, True


@transaction.atomic
def void_voucher(voucher, user=None):
    voucher = Voucher.objects.select_for_update().get(pk=voucher.pk)
    if voucher.status == DocStatus.VOID:
        raise DomainError("Voucher is already void.", "already_void")
    void_journal(voucher.journal, user)
    voucher.status = DocStatus.VOID
    voucher.save(update_fields=["status", "updated_at"])
    return voucher
