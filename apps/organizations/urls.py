from rest_framework.routers import DefaultRouter

from .views import BranchViewSet, MembershipViewSet, OrganizationViewSet

router = DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")
router.register("branches", BranchViewSet, basename="branch")
router.register("members", MembershipViewSet, basename="member")
urlpatterns = router.urls
