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
            .filter(user=user, organization_id=int(raw), organization__is_active=True)
            .first()
        )
        if membership is None:
            return False
        request.organization = membership.organization
        request.membership = membership
        if request.method in SAFE_METHODS:
            return True
        required = getattr(view, "write_roles", WRITE_ROLES)
        if membership.role in required:
            return True
        self.message = "Your role does not permit this action."
        return False
