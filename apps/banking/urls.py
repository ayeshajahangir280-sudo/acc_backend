from rest_framework.routers import DefaultRouter

from .views import BankStatementViewSet

router = DefaultRouter()
router.register("bank-statements", BankStatementViewSet, basename="bank-statement")
urlpatterns = router.urls
