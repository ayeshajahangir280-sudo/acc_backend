from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.errors import DomainError
from apps.core.models import DocStatus
from apps.ledger.models import Account, JournalLine

from . import importer
from .models import BankStatement, StatementLine, StatementMatch

ZERO = Decimal("0.00")


# ------------------------------------------------------------------ helpers
def signed(jl):
    """Effect of a ledger line on the bank account: debit (money in) is positive."""
    return jl.debit - jl.credit


def _effective(qs_prefix=""):
    """Sum of match amounts whose ledger transaction is still posted (matches against voided entries do not count)."""
    return Sum("matches__amount", filter=Q(matches__journal_line__transaction__status=DocStatus.POSTED))


def line_matched(line):
    return line.matches.filter(journal_line__transaction__status=DocStatus.POSTED).aggregate(s=Sum("amount"))["s"] or ZERO


def ledger_cleared(jl):
    return jl.statement_matches.filter(journal_line__transaction__status=DocStatus.POSTED).aggregate(s=Sum("amount"))["s"] or ZERO


def line_status(line, matched):
    return "matched" if matched == abs(line.amount) else "unmatched" if matched == 0 else "partial"


# ------------------------------------------------------------------ import
@transaction.atomic
def import_statement(org, user, *, account, raw, file_name, opening_balance, closing_balance, date_format="iso", skip_duplicates=False):
    if account.organization_id != org.id or not account.is_cash_bank or not account.is_active:
        raise DomainError("Choose an active cash or bank account of this organization.", "invalid_account")
    rows, errors = importer.parse_statement(raw, date_format)
    if errors:
        raise DomainError(f"The file has {len(errors)} invalid row(s); nothing was imported. First problem: row {errors[0]['row']}: {errors[0]['error']}.",
                          "invalid_rows", extra={"errors": errors[:20], "error_count": len(errors)})
    fhash = importer.file_hash(raw)
    if BankStatement.objects.filter(organization=org, account=account, file_hash=fhash).exists():
        raise DomainError("This exact file was already imported for this account.", "duplicate_statement")
    importer.fingerprints(rows)
    known = set(StatementLine.objects.filter(organization=org, account=account, fingerprint__in=[r["fingerprint"] for r in rows])
                .values_list("fingerprint", flat=True))
    skipped = 0
    if known:
        if not skip_duplicates:
            raise DomainError(f"{len(known)} line(s) in this file were already imported from another statement of this account. "
                              "Import non-overlapping periods, or choose to skip duplicates.", "overlapping_statement",
                              extra={"duplicate_lines": len(known)})
        rows, skipped = [r for r in rows if r["fingerprint"] not in known], len(known)
        if not rows:
            raise DomainError("Every line in this file was already imported.", "nothing_new")
    net = sum((r["amount"] for r in rows), ZERO)
    if opening_balance + net != closing_balance:
        raise DomainError(f"The statement does not add up: opening {opening_balance} + lines {net} = {opening_balance + net}, "
                          f"but the closing balance given is {closing_balance}." + (" Duplicates were skipped, so enter the balance at the start of the new lines." if skipped else ""),
                          "statement_not_footing", extra={"computed_closing": str(opening_balance + net)})
    start, end = min(r["date"] for r in rows), max(r["date"] for r in rows)
    try:
        with transaction.atomic():
            st = BankStatement.objects.create(organization=org, account=account, file_name=file_name[:200], file_hash=fhash,
                                              period_start=start, period_end=end, opening_balance=opening_balance,
                                              closing_balance=closing_balance, imported_by=user)
            StatementLine.objects.bulk_create([StatementLine(organization=org, statement=st, account=account, **r) for r in rows])
    except IntegrityError:
        raise DomainError("These lines were imported at the same moment by another request. Reload and check the statements.", "duplicate_statement")
    warnings = []
    prev = (BankStatement.objects.filter(organization=org, account=account, status=BankStatement.Status.RECONCILED, period_end__lt=start)
            .order_by("-period_end").first())
    if prev and prev.closing_balance != opening_balance:
        warnings.append(f"The opening balance {opening_balance} differs from the closing balance {prev.closing_balance} of the last reconciled statement ending {prev.period_end}.")
    return st, {"skipped_duplicates": skipped, "warnings": warnings}


# ------------------------------------------------------------------ matching
def _open_statement(statement):
    st = BankStatement.objects.select_for_update().get(pk=statement.pk)
    if st.status == BankStatement.Status.RECONCILED:
        raise DomainError("This statement is reconciled. Reopen it to change matches.", "statement_reconciled")
    return st


def ledger_candidates(statement, *, sign, days, line_date=None):
    """Posted, non-reversal ledger lines of the statement's account that still have an uncleared amount."""
    centre = line_date
    qs = JournalLine.objects.filter(organization=statement.organization, account=statement.account,
                                    transaction__status=DocStatus.POSTED, transaction__reversal_of__isnull=True)
    if centre is not None:
        qs = qs.filter(date__gte=centre - timedelta(days=days), date__lte=centre + timedelta(days=days))
    else:
        qs = qs.filter(date__gte=statement.period_start - timedelta(days=days), date__lte=statement.period_end + timedelta(days=days))
    qs = qs.filter(debit__gt=0) if sign > 0 else qs.filter(credit__gt=0)
    cleared = Coalesce(Sum("statement_matches__amount", filter=Q(statement_matches__journal_line__transaction__status=DocStatus.POSTED)), ZERO)
    return (qs.annotate(cleared=cleared, size=Case(When(debit__gt=0, then=F("debit")), default=F("credit"), output_field=DecimalField()))
            .annotate(remaining=F("size") - F("cleared")).filter(remaining__gt=0).select_related("transaction", "party"))


@transaction.atomic
def match(statement, user, *, line_id, journal_line_id, amount=None, method="manual"):
    st = _open_statement(statement)
    try:
        line = StatementLine.objects.select_for_update().get(pk=line_id, statement=st, organization=st.organization)
    except StatementLine.DoesNotExist:
        raise DomainError("Statement line not found.", "line_not_found")
    try:
        jl = JournalLine.objects.select_for_update().select_related("transaction").get(pk=journal_line_id, organization=st.organization)
    except JournalLine.DoesNotExist:
        raise DomainError("Ledger entry not found.", "ledger_line_not_found")
    if jl.account_id != st.account_id:
        raise DomainError("That ledger entry belongs to a different account than this statement.", "wrong_account")
    txn = jl.transaction
    if txn.status != DocStatus.POSTED or txn.reversal_of_id is not None:
        raise DomainError("That transaction is void; only posted transactions can be matched.", "transaction_void")
    if (line.amount > 0) != (signed(jl) > 0):
        raise DomainError("A deposit can only match money-in entries and a withdrawal only money-out entries.", "direction_mismatch")
    if StatementMatch.objects.filter(line=line, journal_line=jl).exists():
        raise DomainError("This statement line is already matched to that entry.", "already_matched")
    line_left = abs(line.amount) - line_matched(line)
    ledger_left = abs(signed(jl)) - ledger_cleared(jl)
    if line_left <= 0:
        raise DomainError("This statement line is already fully matched.", "line_fully_matched")
    if ledger_left <= 0:
        raise DomainError("That ledger entry is already fully cleared against a bank statement.", "ledger_already_cleared")
    amt = min(line_left, ledger_left) if amount is None else Decimal(amount)
    if amt <= 0:
        raise DomainError("The matched amount must be greater than zero.", "invalid_amount")
    if amt > line_left or amt > ledger_left:
        raise DomainError(f"The matched amount {amt} is more than what is left: statement line {line_left}, ledger entry {ledger_left}.", "over_match")
    return StatementMatch.objects.create(organization=st.organization, line=line, journal_line=jl, amount=amt, matched_by=user, method=method)


@transaction.atomic
def unmatch(statement, *, match_id=None, line_id=None):
    st = _open_statement(statement)
    qs = StatementMatch.objects.filter(line__statement=st, organization=st.organization)
    qs = qs.filter(pk=match_id) if match_id is not None else qs.filter(line_id=line_id)
    n, _ = qs.delete()
    if not n:
        raise DomainError("Nothing to unmatch.", "match_not_found")
    return n


@transaction.atomic
def auto_match(statement, user, days=7):
    """Match only the unambiguous cases: one ledger entry of exactly the remaining amount within `days`, claimed by one line."""
    st = _open_statement(statement)
    lines = []
    for line in st.lines.select_for_update():
        left = abs(line.amount) - line_matched(line)
        if left > 0:
            lines.append((line, left))
    proposals, claims = {}, {}
    for line, left in lines:
        cands = [c for c in ledger_candidates(st, sign=1 if line.amount > 0 else -1, days=days, line_date=line.date) if c.remaining == left]
        if len(cands) > 1 and line.reference:
            exact = [c for c in cands if line.reference.lower() in (c.transaction.reference or "").lower()]
            cands = exact if len(exact) == 1 else cands
        proposals[line.id] = cands
        for c in cands:
            claims.setdefault(c.id, set()).add(line.id)
    matched = ambiguous = none = 0
    for line, left in lines:
        cands = proposals[line.id]
        if not cands:
            none += 1
        elif len(cands) == 1 and len(claims[cands[0].id]) == 1:
            match(st, user, line_id=line.id, journal_line_id=cands[0].id, method="auto")
            matched += 1
        else:
            ambiguous += 1
    return {"matched": matched, "ambiguous": ambiguous, "no_candidate": none}


# ------------------------------------------------------------------ status
def summary(st):
    lines = list(st.lines.annotate(eff=Coalesce(_effective(), ZERO)))
    stale = StatementMatch.objects.filter(line__statement=st).exclude(journal_line__transaction__status=DocStatus.POSTED).count()
    counts = {"matched": 0, "partial": 0, "unmatched": 0}
    net_matched = ZERO
    for l in lines:
        counts[line_status(l, l.eff)] += 1
        net_matched += l.eff if l.amount > 0 else -l.eff
    difference = st.closing_balance - st.opening_balance - net_matched
    ledger = JournalLine.objects.filter(organization=st.organization, account=st.account, date__lte=st.period_end,
                                        transaction__status=DocStatus.POSTED)
    book = ledger.aggregate(d=Sum("debit"), c=Sum("credit"))
    uncleared = [c for c in ledger_candidates(st, sign=1, days=0).filter(date__lte=st.period_end)] + \
                [c for c in ledger_candidates(st, sign=-1, days=0).filter(date__lte=st.period_end)]
    uncleared_net = sum((signed(c) / abs(signed(c)) * c.remaining for c in uncleared), ZERO)
    return {
        "line_count": len(lines), "matched_lines": counts["matched"], "partial_lines": counts["partial"], "unmatched_lines": counts["unmatched"],
        "stale_matches": stale, "statement_net": sum((l.amount for l in lines), ZERO), "matched_net": net_matched,
        "difference": difference, "can_complete": st.status == BankStatement.Status.IN_PROGRESS and difference == 0 and stale == 0
        and counts["unmatched"] == 0 and counts["partial"] == 0 and bool(lines),
        "book_balance_at_period_end": (book["d"] or ZERO) - (book["c"] or ZERO),
        "uncleared_book_items": len(uncleared), "uncleared_book_net": uncleared_net,
    }


@transaction.atomic
def complete(statement, user):
    st = _open_statement(statement)
    s = summary(st)
    if s["stale_matches"]:
        raise DomainError("Some matched transactions were voided. Unmatch those lines first.", "stale_matches")
    if s["unmatched_lines"] or s["partial_lines"]:
        raise DomainError("Every statement line must be fully matched before reconciling.", "lines_unmatched")
    if s["difference"] != 0:
        raise DomainError(f"The difference is {s['difference']}; it must be zero to reconcile.", "difference_not_zero")
    st.status, st.reconciled_by, st.reconciled_at = BankStatement.Status.RECONCILED, user, timezone.now()
    st.save(update_fields=["status", "reconciled_by", "reconciled_at", "updated_at"])
    return st


@transaction.atomic
def reopen(statement, user):
    st = BankStatement.objects.select_for_update().get(pk=statement.pk)
    if st.status != BankStatement.Status.RECONCILED:
        raise DomainError("This statement is not reconciled.", "not_reconciled")
    later = BankStatement.objects.filter(organization=st.organization, account=st.account, status=BankStatement.Status.RECONCILED).filter(
        Q(period_end__gt=st.period_end) | Q(period_end=st.period_end, id__gt=st.id))
    if later.exists():
        raise DomainError("A later statement of this account is reconciled. Reopen that one first.", "later_reconciled")
    st.status, st.reconciled_by, st.reconciled_at = BankStatement.Status.IN_PROGRESS, None, None
    st.save(update_fields=["status", "reconciled_by", "reconciled_at", "updated_at"])
    return st


@transaction.atomic
def discard(statement):
    st = _open_statement(statement)
    st.delete()


def assert_not_reconciled(txn):
    """Called by the ledger before a transaction is voided: a reconciled transaction is part of a closed reconciliation."""
    if StatementMatch.objects.filter(journal_line__transaction=txn, line__statement__status=BankStatement.Status.RECONCILED).exists():
        raise DomainError("This transaction was reconciled with a bank statement. Reopen that reconciliation before voiding it.", "transaction_reconciled")
