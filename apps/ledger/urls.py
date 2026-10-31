from rest_framework.routers import DefaultRouter

from .views import AccountViewSet, JournalViewSet, PartyBalanceViewSet

router = DefaultRouter()
router.register("accounts", AccountViewSet, basename="account")
router.register("journal", JournalViewSet, basename="journal")
router.register("party-balances", PartyBalanceViewSet, basename="party-balance")
urlpatterns = router.urls
