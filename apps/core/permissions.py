from rest_framework.permissions import SAFE_METHODS, BasePermission

ORG_HEADER = "HTTP_X_ORGANIZATION_ID"
WRITE_ROLES = {"owner", "admin", "accountant"}
ADMIN_ROLES = {"owner", "admin"}


class IsOrgMember(BasePermission):
    """Resolves the active organization from the X-Organization-ID header and checks membership.

    Sets request.organization / request.membership. Viewers are read-only.
    """

    message = "You do not have access to this organization."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        raw = request.META.get(ORG_HEADER)
        if not raw or not raw.isdigit():
            self.message = "X-Organization-ID header is required."
            return False
        from apps.organizations.models import Membership

        membership = (
            Membership.objects.select_related("organization")
            .filter(user=user, organization_id=int(raw), organization__is_active=True, is_active=True)
            .first()
        )
        if membership is None:
            return False
        request.organization = membership.organization
        request.membership = membership
        from . import perms

        try:
            need = perms.resolve(request, view)
        except perms.Unmapped:
            # Fail closed: an endpoint that is not in the permission table cannot be used.
            self.message = "This action is not permitted."
            return False
        if need is None or perms.has(membership, *need):
            return True
        module, action = need
        labels = dict((m[0], m[1]) for m in perms.MODULES)
        label = " or ".join(labels.get(m, m) for m in module) if isinstance(module, tuple) else labels.get(module, module)
        self.message = f"You do not have permission to {action} in {label}."
        return False
