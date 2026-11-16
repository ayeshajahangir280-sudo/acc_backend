from rest_framework.routers import DefaultRouter

from .views import BranchAccessViewSet, BranchViewSet, MembershipViewSet, InvitationViewSet, OrganizationViewSet

router = DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("branches", BranchViewSet, basename="branch")
router.register("members", MembershipViewSet, basename="member")
router.register("invitations", InvitationViewSet, basename="invitation")
router.register("branch-access", BranchAccessViewSet, basename="branch-access")
from django.urls import path

from .views import PermissionCatalogView

urlpatterns = router.urls + [path("permissions/catalog/", PermissionCatalogView.as_view())]
