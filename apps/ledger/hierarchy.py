"""Chart-of-accounts hierarchy (sub-accounts).

Rules (docs/ASSUMPTIONS.md A-series): a sub-account belongs to the same organization and has the same account type as
its parent; the tree is at most MAX_DEPTH levels deep; no cycles; a parent cannot be deactivated while it has active
children. Parents remain postable (as in Zoho Books). Re-parenting locks the organization row so two concurrent moves
cannot combine into a cycle.
"""
from django.db import transaction

from apps.core.errors import DomainError

from .models import Account

MAX_DEPTH = 5  # levels including the root: 1 = top-level account


def depth_of(account, by_id):
    d, seen = 1, {account.id}
    p = account.parent_id
    while p is not None:
        d += 1
        if p in seen:  # defensive: never loop on bad data
            break
        seen.add(p)
        p = by_id[p].parent_id if p in by_id else None
    return d


def _subtree_height(account_id, children):
    h = 1
    for c in children.get(account_id, ()):
        h = max(h, 1 + _subtree_height(c, children))
    return h


def check_parent(org, account, parent, *, acc_type=None):
    """Validate attaching `account` (existing or None for a new one) under `parent`. Caller holds the org lock."""
    if parent is None:
        return
    if parent.organization_id != org.id:
        raise DomainError("Parent account not found.", "invalid_parent")
    if account is not None and account.pk == parent.pk:
        raise DomainError("An account cannot be its own parent.", "parent_cycle")
    if not parent.is_active:
        raise DomainError("The parent account is inactive.", "invalid_parent")
    if (acc_type or (account.type if account else parent.type)) != parent.type:
        raise DomainError("A sub-account must have the same type as its parent.", "parent_type_mismatch")
    rows = list(Account.objects.filter(organization=org).values_list("id", "parent_id"))
    by_id = {i: p for i, p in rows}
    # walk up from the parent: hitting `account` means a cycle
    d, cur, seen = 1, parent.pk, set()
    while cur is not None and cur not in seen:
        if account is not None and cur == account.pk:
            raise DomainError("That parent would create a loop in the account tree.", "parent_cycle")
        seen.add(cur)
        cur = by_id.get(cur)
        if cur is not None:
            d += 1
    children = {}
    for i, p in rows:
        if p is not None:
            children.setdefault(p, []).append(i)
    below = _subtree_height(account.pk, children) if account is not None else 1
    if d + below > MAX_DEPTH:
        raise DomainError(f"Accounts can be nested at most {MAX_DEPTH} levels deep.", "parent_too_deep")


def lock_org(org):
    from apps.organizations.models import Organization

    Organization.objects.select_for_update().only("id").get(pk=org.pk)


@transaction.atomic
def create_account(org, **fields):
    parent = fields.get("parent")
    if parent is not None:
        lock_org(org)
        parent = Account.objects.select_for_update().get(pk=parent.pk)
        check_parent(org, None, parent, acc_type=fields.get("type"))
    return Account.objects.create(organization=org, **fields)


@transaction.atomic
def update_account(org, account, **fields):
    lock_org(org)
    account = Account.objects.select_for_update().get(pk=account.pk)
    new_parent = fields["parent"] if "parent" in fields else account.parent
    new_type = fields.get("type", account.type)
    if "type" in fields and new_type != account.type:
        if Account.objects.filter(organization=org, parent=account).exists():
            raise DomainError("Change the type of the sub-accounts first, or move them.", "parent_type_mismatch")
    if "parent" in fields or "type" in fields:
        check_parent(org, account, new_parent, acc_type=new_type)
    if fields.get("is_active") is False and account.is_active:
        if Account.objects.filter(organization=org, parent=account, is_active=True).exists():
            raise DomainError("Deactivate or move the active sub-accounts first.", "has_active_children")
    if fields.get("is_active") is True and not account.is_active and new_parent is not None and not new_parent.is_active:
        raise DomainError("Reactivate the parent account first.", "invalid_parent")
    for k, v in fields.items():
        setattr(account, k, v)
    account.save()
    return account
