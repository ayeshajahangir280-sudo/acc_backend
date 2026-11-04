from decimal import Decimal

from django.db import transaction

from apps.core.errors import DomainError
from apps.core.idempotency import find_existing, retry_on_key_conflict
from apps.core.models import DocStatus, next_number
from apps.core.money import money
from apps.ledger.services import Line, post_journal, void_journal

from . import rules
from .models import Voucher, VoucherLine

PREFIX = {"general": "GV", "journal": "JV", "contra": "CV", "opening": "OB"}
OFFSET_CODE = "OPENING_OFFSET"


def _offset_account(org):
    """Equity account that absorbs the difference between the opening balances entered (Zoho: 'Opening Balance Offset')."""
    from apps.ledger.models import Account, AccountType

    acct = Account.objects.filter(organization=org, system_code=OFFSET_CODE).first()
    if acct:
        return acct
    code = next(str(c) for c in range(3900, 3999) if not Account.objects.filter(organization=org, code=str(c)).exists())
    return Account.objects.create(organization=org, code=code, name="Opening Balance Offset", type=AccountType.EQUITY, system_code=OFFSET_CODE)


def _check_opening(org, date):
    from apps.ledger.models import JournalTransaction

    if Voucher.objects.filter(organization=org, type="opening", status=DocStatus.POSTED).exists():
        raise DomainError("Opening balances are already entered. Void them first to enter them again.", "opening_exists")
    earlier = JournalTransaction.objects.filter(organization=org, status="posted", date__lt=date).exclude(source_type="voucher.opening")
    if earlier.exists():
        raise DomainError("Opening balances must be dated on or before the first transaction in the books.", "opening_after_transactions")


@retry_on_key_conflict
@transaction.atomic
def create_voucher(org, user, vtype, *, date, lines, narration="", branch=None, idempotency_key=None):
    """lines: list of dicts {account, debit, credit, party?, description?}. Balance is enforced by the ledger."""
    existing = find_existing(Voucher, org, idempotency_key)
    if existing:
        return existing, False
    if vtype not in PREFIX:
        raise DomainError("Unknown voucher type.", "invalid_type")
    rules.validate(vtype, lines)
    jl = [Line(l["account"], money(l.get("debit") or 0), money(l.get("credit") or 0), l.get("party"), l.get("description", ""))
          for l in lines]
    if vtype == "opening":
        _check_opening(org, date)
        diff = sum((l.debit - l.credit for l in jl), Decimal("0"))
        if diff:  # the books must balance: the difference goes to the offset account (credit if debits exceed credits)
            jl.append(Line(_offset_account(org), debit=-diff if diff < 0 else Decimal("0"), credit=diff if diff > 0 else Decimal("0")))
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
