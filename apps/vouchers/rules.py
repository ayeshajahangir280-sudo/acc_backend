"""Voucher-type rules layered on top of the ledger's own balance validation (debit == credit).

Purposes:
  journal : standard accounting adjustments / journal entries (any accounts).
  general : transactions that do NOT fit a dedicated workflow, so it may not touch the
            receivable/payable control accounts (use sales/purchase/receipt/payment) and may not be a
            pure cash/bank transfer (use contra).
  contra  : transfers between two different cash/bank accounts only.
"""
from apps.core.errors import DomainError

CONTROL = {"ACCOUNTS_RECEIVABLE", "ACCOUNTS_PAYABLE"}


def _common(lines):
    for l in lines:
        is_control = l["account"].system_code in CONTROL
        if is_control and l.get("party") is None:
            raise DomainError("Lines on receivable/payable accounts must name a party.", "party_required")
        if not is_control and l.get("party") is not None:
            raise DomainError("A party may only be set on receivable/payable lines.", "party_not_allowed")


def _journal(lines):
    pass


def _general(lines):
    if any(l["account"].system_code in CONTROL for l in lines):
        raise DomainError(
            "General vouchers cannot post to receivable/payable accounts; use the Sales, Purchase, Receipt or "
            "Payment workflows (or a Journal Voucher for adjustments).", "general_control_account")
    if all(l["account"].is_cash_bank for l in lines):
        raise DomainError("Transfers between cash/bank accounts belong in a Contra Voucher.", "general_use_contra")


def _contra(lines):
    if any(not l["account"].is_cash_bank for l in lines):
        raise DomainError("Contra vouchers may only use cash/bank accounts.", "contra_accounts")
    if len({l["account"].id for l in lines}) < 2:
        raise DomainError("A contra voucher must transfer between two different accounts.", "contra_same_account")


RULES = {"journal": _journal, "general": _general, "contra": _contra}


def validate(vtype, lines):
    # A General voucher's purpose rule goes first so the user is told to use the dedicated screen
    # (the most helpful message) before any generic line-level complaint such as a missing party.
    if vtype == "general":
        _general(lines)
    _common(lines)
    RULES[vtype](lines)
