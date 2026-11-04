"""Branch-level access rules. The single place that decides which branches a user may reach.

Rules
- owner / admin: every branch of the organization (scope = None), including documents with no branch.
- accountant / viewer: only branches assigned through BranchAccess. No assignments = no branch data at all.
  Documents without a branch are not visible to them, and they must choose an assigned branch when posting.
- No membership: nothing (superusers are platform operators and are not restricted).
"""
from apps.core.errors import DomainError
from apps.core.permissions import ADMIN_ROLES

from .models import BranchAccess, Membership

NONE_SCOPE = frozenset()


def scope_for_membership(membership):
    """None = unrestricted; otherwise a frozenset of branch ids (possibly empty). Cached on the membership object."""
    if membership.role in ADMIN_ROLES:
        return None
    if not hasattr(membership, "_branch_scope"):
        membership._branch_scope = frozenset(
            BranchAccess.objects.filter(membership=membership, branch__organization_id=membership.organization_id)
            .values_list("branch_id", flat=True)
        )
    return membership._branch_scope


def scope_for_user(org, user):
    """Scope for service-layer checks, where only (org, user) are known."""
    if user is None or getattr(user, "is_superuser", False):
        return None  # internal calls and platform operators
    membership = Membership.objects.filter(user=user, organization=org).first()
    return NONE_SCOPE if membership is None else scope_for_membership(membership)


def request_scope(request):
    return scope_for_membership(request.membership)


def scope_queryset(qs, request, field="branch_id"):
    """Restrict a queryset to the caller's branches. Unbranched rows are excluded for restricted users."""
    scope = request_scope(request)
    return qs if scope is None else qs.filter(**{f"{field}__in": scope})


def branch_visible(request, branch_id):
    scope = request_scope(request)
    return scope is None or branch_id in scope


def assert_can_post(org, user, branch):
    """Posting guard (called from the ledger posting engine)."""
    if branch is not None and not branch.is_active:
        raise DomainError("This branch is inactive.", "inactive_branch")
    scope = scope_for_user(org, user)
    if scope is None:
        return
    if branch is None:
        raise DomainError("Choose one of your branches for this document.", "branch_required")
    if branch.id not in scope:
        raise DomainError("You do not have access to this branch.", "branch_forbidden")


def assert_can_void(org, user, branch_id):
    scope = scope_for_user(org, user)
    if scope is not None and branch_id not in scope:
        raise DomainError("You do not have access to this branch.", "branch_forbidden")


def report_branch(request):
    """Branch filter for reports and other aggregates.

    Returns None (all branches) or a frozenset of branch ids. Restricted users are always confined to their own
    branches, and naming a branch outside them is refused (403) rather than silently ignored.
    """
    from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

    from .models import Branch

    scope = request_scope(request)
    raw = request.query_params.get("branch")
    if not raw:
        return scope
    if not raw.isdigit():
        raise ValidationError({"branch": "Must be a number."})
    branch_id = int(raw)
    if scope is not None:
        if branch_id not in scope:
            raise PermissionDenied("You do not have access to this branch.")
    elif not Branch.objects.filter(pk=branch_id, organization=request.organization).exists():
        raise NotFound("Branch not found.")
    return frozenset({branch_id})
