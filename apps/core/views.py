from rest_framework import mixins, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from django.contrib.auth import authenticate

from .permissions import IsOrgMember


class OrgScopedMixin:
    """Restricts every queryset to request.organization and injects it on create."""

    permission_classes = [IsOrgMember]

    def get_queryset(self):
        return super().get_queryset().filter(organization=self.request.organization)

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
        user = authenticate(username=request.data.get("username"), password=request.data.get("password"))
        if user is None or not user.is_active:
            return Response({"detail": "Invalid credentials."}, status=400)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=204)
