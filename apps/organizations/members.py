"""Organization membership management: invitations, adding existing users, role / status changes, removal.

The rules live here (not in views) so no endpoint can bypass them:
- Only active members count anywhere; a deactivated membership has no access.
- Nobody changes or removes their own membership (no self-promotion, no accidental self-lock-out).
- Only an owner can create, change or remove an owner.
- An organization always keeps at least one active owner.
- No privilege escalation: a non-owner can grant only permissions they hold themselves, and can change or remove only
  members whose access does not exceed their own.
- Users, organizations and accounting records are never deleted; removal deletes only the membership row.
- Every change is written to the access audit trail (AccessChange): who, when, and what changed.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.core.errors import DomainError, Forbidden

from .models import AccessChange, Invitation, Membership, Organization

INVITE_DAYS = 7
OWNER = Membership.Role.OWNER


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def active_membership(user, org_id):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return Membership.objects.filter(user=user, organization_id=org_id, is_active=True, organization__is_active=True).first()


def _lock(org):
    return Organization.objects.select_for_update().only("id").get(pk=org.pk if hasattr(org, "pk") else org)


def _active_owner_count(org):
    return Membership.objects.filter(organization=org, role=OWNER, is_active=True).count()


def _check_actor(actor, role=None, target=None):
    """actor: the acting Membership. role: role being granted (if any). target: Membership being changed (if any)."""
    if actor is None or not actor.is_active:
        raise Forbidden("You are not an active member of this organization.", "forbidden")
    if target is not None and target.user_id == actor.user_id:
        raise Forbidden("You cannot change or remove your own membership. Ask another owner.", "self_change")
    if actor.role != OWNER and (role == OWNER or (target is not None and target.role == OWNER)):
        raise Forbidden("Only an owner can add, change or remove an owner.", "owner_only")


def _check_authority(actor, target):
    """A non-owner may only change or remove a member whose access (as if active) does not exceed their own: otherwise a
    member with user management but fewer rights could strip, deactivate or reshape someone who outranks them."""
    from apps.core import perms

    if actor.role == OWNER:
        return
    probe = Membership(role=target.role, is_active=True, permissions=target.permissions)
    extra = perms.exceeds(perms.effective(probe), perms.effective(actor))
    if extra:
        raise Forbidden("You cannot change a member who has permissions you do not have yourself: " + ", ".join(extra[:6])
                        + ("…" if len(extra) > 6 else ""), "outranks_actor")


def _snapshot(m):
    """What an audit diff compares: role, active flag, whether custom, and effective permissions (as if active)."""
    from apps.core import perms

    probe = Membership(role=m.role, is_active=True, permissions=m.permissions)
    return {"role": m.role, "is_active": m.is_active, "custom": m.role != OWNER and m.permissions is not None,
            "perms": perms.flat(perms.effective(probe))}


def _sorted_perms(items):
    from apps.core import perms

    def key(s):
        mod, act = s.split(".", 1)
        return (perms.MODULE_KEYS.index(mod) if mod in perms.MODULE_KEYS else 99, perms.ACTIONS.index(act) if act in perms.ACTIONS else 99)

    return sorted(items, key=key)


def _record(org, actor_user, event, *, target_user=None, target_name="", membership_id=None, details=None):
    AccessChange.objects.create(
        organization_id=org.pk if hasattr(org, "pk") else org, event=event, actor=actor_user,
        actor_name=actor_user.get_username() if actor_user is not None else "", target_user=target_user,
        target_name=target_name or (target_user.get_username() if target_user is not None else ""),
        membership_id=membership_id, details=details or {})


def _record_diff(org, actor_user, target, before, after):
    """One audit row per kind of change (role or permissions, status), each carrying the effective-permission diff."""
    granted = _sorted_perms(after["perms"] - before["perms"])
    revoked = _sorted_perms(before["perms"] - after["perms"])
    common = {"target_user": target.user, "membership_id": target.pk}
    perm_diff = {k: v for k, v in (("granted", granted), ("revoked", revoked)) if v}
    if before["role"] != after["role"]:
        _record(org, actor_user, AccessChange.Event.ROLE_CHANGED,
                details={"role": [before["role"], after["role"]], "custom": [before["custom"], after["custom"]], **perm_diff}, **common)
    elif perm_diff or before["custom"] != after["custom"]:
        _record(org, actor_user, AccessChange.Event.PERMISSIONS_CHANGED,
                details={"custom": [before["custom"], after["custom"]], **perm_diff}, **common)
    if before["is_active"] != after["is_active"]:
        event = AccessChange.Event.ACTIVATED if after["is_active"] else AccessChange.Event.DEACTIVATED
        _record(org, actor_user, event, details={"is_active": [before["is_active"], after["is_active"]]}, **common)


def _check_grant(actor, role, permissions):
    """No privilege escalation: everything the target would be able to do must be something the actor can do."""
    from apps.core import perms

    probe = Membership(role=role, is_active=True, permissions=permissions)
    target_perms = perms.effective(probe)
    mine = perms.effective(actor)
    extra = sorted(f"{m}.{a}" for m, acts in target_perms.items() for a in acts if a not in mine.get(m, set()))
    if extra:
        raise Forbidden("You cannot grant permissions you do not have yourself: " + ", ".join(extra[:6]) + ("…" if len(extra) > 6 else ""),
                        "permission_escalation")


def _clean_permissions(raw):
    from apps.core import perms

    if raw is None:
        return None
    try:
        return perms.as_lists(perms.normalize(raw))
    except ValueError as e:
        raise DomainError(str(e), "invalid_permissions")


def _valid_role(role):
    if role not in Membership.Role.values:
        raise DomainError("Unknown role.", "invalid_role")
    return role


def _find_user_by_email(email):
    users = list(get_user_model().objects.filter(email__iexact=email, is_active=True)[:2])
    return users[0] if len(users) == 1 else None


@transaction.atomic
def invite(org, actor, *, email, role, permissions=None):
    """Add an existing user directly, or create an invitation. Returns (kind, obj, raw_token)."""
    _lock(org)
    email = (email or "").strip()
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError

    try:
        validate_email(email)
    except ValidationError:
        raise DomainError("Enter a valid email address.", "invalid_email")
    _check_actor(actor, role=_valid_role(role))
    permissions = _clean_permissions(permissions)
    _check_grant(actor, role, permissions)
    user = _find_user_by_email(email)
    if user is not None:
        existing = Membership.objects.filter(organization=org, user=user).first()
        if existing is not None:
            raise DomainError("This person is already a member" + ("" if existing.is_active else " (inactive: reactivate them instead)") + ".",
                              "already_member")
        m = Membership.objects.create(organization=org, user=user, role=role, permissions=permissions if role != OWNER else None,
                                      invited_by=actor.user)
        snap = _snapshot(m)
        _record(org, actor.user, AccessChange.Event.MEMBER_ADDED, target_user=user, membership_id=m.pk,
                details={"role": [None, role], "custom": [False, snap["custom"]], "granted": _sorted_perms(snap["perms"])})
        return "member", m, None
    if Invitation.objects.filter(organization=org, email__iexact=email, accepted_at__isnull=True, cancelled_at__isnull=True,
                                 expires_at__gt=timezone.now()).exists():
        raise DomainError("An invitation is already pending for this email. Resend it instead.", "already_invited")
    raw = secrets.token_urlsafe(32)
    inv = Invitation.objects.create(organization=org, email=email, role=role, permissions=permissions if role != OWNER else None,
                                    token_hash=hash_token(raw), invited_by=actor.user,
                                    expires_at=timezone.now() + timedelta(days=INVITE_DAYS))
    _record(org, actor.user, AccessChange.Event.INVITED, target_name=email,
            details={"role": [None, role], "custom": [False, role != OWNER and inv.permissions is not None]})
    return "invitation", inv, raw


@transaction.atomic
def resend(inv, actor):
    """New token and expiry (the old link stops working)."""
    _lock(inv.organization_id)
    inv = Invitation.objects.select_for_update().get(pk=inv.pk)
    _check_actor(actor, role=inv.role)
    if inv.accepted_at or inv.cancelled_at:
        raise DomainError("This invitation is no longer open.", "invitation_closed")
    raw = secrets.token_urlsafe(32)
    inv.token_hash = hash_token(raw)
    inv.expires_at = timezone.now() + timedelta(days=INVITE_DAYS)
    inv.save(update_fields=["token_hash", "expires_at", "updated_at"])
    return inv, raw


@transaction.atomic
def cancel(inv, actor):
    inv = Invitation.objects.select_for_update().get(pk=inv.pk)
    _check_actor(actor, role=inv.role)
    if inv.accepted_at:
        raise DomainError("This invitation was already accepted.", "invitation_closed")
    if not inv.cancelled_at:
        inv.cancelled_at = timezone.now()
        inv.save(update_fields=["cancelled_at", "updated_at"])
        _record(inv.organization_id, actor.user, AccessChange.Event.INVITATION_CANCELLED, target_name=inv.email,
                details={"role": [inv.role, None]})
    return inv


UNSET = object()


@transaction.atomic
def update_member(org, actor, target, *, role=None, is_active=None, permissions=UNSET):
    """permissions: UNSET = leave as is; None = back to the role preset; dict = custom permissions."""
    _lock(org)
    target = Membership.objects.select_for_update().get(pk=target.pk, organization=org)
    new_role = _valid_role(role) if role is not None else target.role
    _check_actor(actor, role=new_role if role is not None else None, target=target)
    _check_authority(actor, target)
    before = _snapshot(target)
    new_perms = target.permissions if permissions is UNSET else _clean_permissions(permissions)
    if new_role == OWNER:
        new_perms = None  # owners always have everything
    if role is not None or permissions is not UNSET:
        _check_grant(actor, new_role, new_perms)
    losing_owner = target.role == OWNER and target.is_active and (new_role != OWNER or is_active is False)
    if losing_owner and _active_owner_count(org) <= 1:
        raise DomainError("An organization must keep at least one active owner.", "last_owner")
    fields = []
    if role is not None and role != target.role:
        target.role = new_role
        fields.append("role")
    if new_perms != target.permissions:
        target.permissions = new_perms
        fields.append("permissions")
    if is_active is not None and is_active != target.is_active:
        target.is_active = is_active
        fields.append("is_active")
    if fields:
        target.save(update_fields=fields + ["updated_at"])
        _record_diff(org, actor.user, target, before, _snapshot(target))
    return target


@transaction.atomic
def remove_member(org, actor, target):
    _lock(org)
    target = Membership.objects.select_for_update().get(pk=target.pk, organization=org)
    _check_actor(actor, target=target)
    _check_authority(actor, target)
    if target.role == OWNER and target.is_active and _active_owner_count(org) <= 1:
        raise DomainError("An organization must keep at least one active owner.", "last_owner")
    snap = _snapshot(target)
    _record(org, actor.user, AccessChange.Event.REMOVED, target_user=target.user, membership_id=target.pk,
            details={"role": [target.role, None], "revoked": _sorted_perms(snap["perms"]) if target.is_active else []})
    target.delete()  # membership only; the user and every record they created stay


def find_invitation(raw):
    inv = Invitation.objects.select_related("organization").filter(token_hash=hash_token(raw or "")).first()
    if inv is None or inv.status != "pending" or not inv.organization.is_active:
        return None
    return inv


@transaction.atomic
def accept(raw, *, username="", password="", email_login_user=None):
    """Accept an invitation. New person: creates an account (username + password). If an account with the invited email
    already exists, `email_login_user` must be that account (authenticated by the view). Returns the Membership."""
    inv = find_invitation(raw)
    if inv is None:
        raise DomainError("This invitation link is invalid, expired or already used.", "invitation_invalid")
    inv = Invitation.objects.select_for_update().get(pk=inv.pk)
    if inv.status != "pending":
        raise DomainError("This invitation link is invalid, expired or already used.", "invitation_invalid")
    User = get_user_model()
    existing = _find_user_by_email(inv.email)
    if existing is not None:
        if email_login_user is None or email_login_user.pk != existing.pk:
            raise DomainError("An account already exists for this email. Sign in with it to accept.", "account_exists")
        user = existing
    else:
        username = (username or "").strip()
        if not username or len(username) > 150:
            raise DomainError("Choose a username.", "invalid_username")
        if User.objects.filter(username__iexact=username).exists():
            raise DomainError("That username is taken.", "username_taken")
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        try:
            validate_password(password, User(username=username, email=inv.email))
        except ValidationError as e:
            raise DomainError(" ".join(e.messages), "weak_password")
        user = User.objects.create_user(username=username, email=inv.email, password=password)
    m, created = Membership.objects.get_or_create(organization=inv.organization, user=user,
                                                  defaults={"role": inv.role, "permissions": inv.permissions, "invited_by": inv.invited_by})
    if not created and not m.is_active:
        raise DomainError("Your membership in this organization was deactivated. Ask an owner to reactivate it.", "membership_inactive")
    inv.accepted_at = timezone.now()
    inv.accepted_by = user
    inv.save(update_fields=["accepted_at", "accepted_by", "updated_at"])
    if created:
        snap = _snapshot(m)
        _record(inv.organization_id, user, AccessChange.Event.INVITATION_ACCEPTED, target_user=user, membership_id=m.pk,
                details={"role": [None, m.role], "custom": [False, snap["custom"]], "granted": _sorted_perms(snap["perms"])})
    return m


def invite_link(request, raw):
    """Absolute link when the frontend origin is known (FRONTEND_URL, else a CORS-allowed Origin), plus the relative path."""
    path = f"/accept-invite#token={raw}"
    base = getattr(settings, "FRONTEND_URL", "") or ""
    origin = request.META.get("HTTP_ORIGIN", "") if request is not None else ""
    if not base and origin and origin in getattr(settings, "CORS_ALLOWED_ORIGINS", []):
        base = origin
    return (base.rstrip("/") + path) if base else "", path


def send_invite_email(org, inv, url):
    """Best effort. Returns True if sent. Never raises (the copyable link always works)."""
    from apps.documents import mail
    from django.core.mail import EmailMessage

    if not url or not mail.is_configured():
        return False
    body = (f"You have been invited to join {org.name} on Accounting Pro as {inv.get_role_display()}.\n\n"
            f"Open this link to accept (valid for {INVITE_DAYS} days):\n{url}\n\nIf you did not expect this, ignore this email.")
    try:
        EmailMessage(subject=f"Invitation to {org.name}", body=body, from_email=settings.DEFAULT_FROM_EMAIL or None, to=[inv.email]).send()
        return True
    except Exception:
        return False
