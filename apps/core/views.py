from rest_framework import mixins, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from django.contrib.auth import authenticate, get_user_model

from .permissions import IsOrgMember


class OrgScopedMixin:
    """Restricts every queryset to request.organization and injects it on create."""

    permission_classes = [IsOrgMember]
    branch_scoped = False  # set True on viewsets whose model has a `branch` FK; restricted users then see only their branches

    def get_queryset(self):
        qs = super().get_queryset().filter(organization=self.request.organization)
        if self.branch_scoped:
            from apps.organizations.access import scope_queryset

            qs = scope_queryset(qs, self.request)
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["organization"] = getattr(self.request, "organization", None)
        return ctx


class OrgReadOnlyViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    pass


class OrgCreateReadViewSet(OrgScopedMixin, mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Posted accounting documents: create / list / retrieve only. Corrections go through void."""


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        if "@" in username:  # allow signing in with the email address too
            match = get_user_model().objects.filter(email__iexact=username).first()
            if match:
                username = match.get_username()
        user = authenticate(username=username, password=request.data.get("password"))
        if user is None or not user.is_active:
            return Response({"detail": "Invalid credentials."}, status=400)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=204)


class MeView(APIView):
    """The signed-in user and their active memberships."""

    def get(self, request):
        from apps.organizations.models import Membership

        ms = Membership.objects.filter(user=request.user, is_active=True, organization__is_active=True).select_related("organization")
        u = request.user
        return Response({"id": u.id, "username": u.get_username(), "email": u.email,
                         "memberships": [{"organization": m.organization_id, "organization_name": m.organization.name, "role": m.role} for m in ms]})


class InvitationLookupView(APIView):
    """Public: what an invitation token is for (POST so the token never sits in a URL or access log)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        from apps.organizations.members import _find_user_by_email, find_invitation

        inv = find_invitation(str(request.data.get("token") or ""))
        if inv is None:
            return Response({"detail": "This invitation link is invalid, expired or already used.", "code": "invitation_invalid"}, status=404)
        return Response({"organization_name": inv.organization.name, "email": inv.email, "role": inv.role,
                         "expires_at": inv.expires_at, "existing_account": _find_user_by_email(inv.email) is not None})


class InvitationAcceptView(APIView):
    """Public: accept an invitation. New people choose a username and password; someone who already has an account for the
    invited email signs in with it. Returns an auth token so the person is signed in straight away."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        from apps.organizations import members

        raw = str(request.data.get("token") or "")
        inv = members.find_invitation(raw)
        login_user = None
        if inv is not None and members._find_user_by_email(inv.email) is not None:
            login_user = authenticate(username=(request.data.get("username") or "").strip(), password=request.data.get("password"))
            if login_user is None:
                match = members._find_user_by_email(inv.email)
                login_user = authenticate(username=match.get_username(), password=request.data.get("password"))
            if login_user is None:
                return Response({"detail": "Wrong username or password for the existing account.", "code": "invalid_credentials"}, status=400)
        m = members.accept(raw, username=request.data.get("username") or "", password=request.data.get("password") or "",
                           email_login_user=login_user)
        token, _ = Token.objects.get_or_create(user=m.user)
        return Response({"token": token.key, "organization": m.organization_id}, status=201)
