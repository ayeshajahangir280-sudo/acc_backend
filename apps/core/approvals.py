"""Approval workflow shared by expenses and purchase bills.

Defaults (see docs/ASSUMPTIONS.md): off. When on, a document whose total is >= approval_min_amount is saved as `pending`
with no journal and no stock movement. Owners and admins approve or reject; an admin may not approve a document they
created themselves (the owner may, otherwise a one-person organisation could never post). Approval posts with the
document's own date, so the period lock, branch rules and costing run at approval time, not before."""
from django.utils import timezone

from apps.core.errors import DomainError
from apps.core.models import ApprovalStatus

APPROVER_ROLES = {"owner", "admin"}


def needs_approval(org, kind, total):
    flag = org.expense_approval_required if kind == "expense" else org.bill_approval_required
    return bool(flag) and total >= org.approval_min_amount


def check_can_decide(membership, doc):
    if membership is None or membership.role not in APPROVER_ROLES:
        raise DomainError("Only owners and admins can approve or reject.", "approval_forbidden")
    if doc.status != ApprovalStatus.PENDING:
        raise DomainError("This document is not waiting for approval.", "not_pending")


def check_self_approval(membership, user, doc):
    if doc.created_by_id == user.id and membership.role != "owner":
        raise DomainError("You cannot approve a document you created.", "self_approval")


def mark_approved(doc, user):
    doc.status = ApprovalStatus.POSTED
    doc.approved_by = user
    doc.approved_at = timezone.now()


def mark_rejected(doc, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("Give a reason for rejecting.", "reason_required")
    doc.status = ApprovalStatus.REJECTED
    doc.approved_by = user
    doc.approved_at = timezone.now()
    doc.rejection_reason = reason[:300]
