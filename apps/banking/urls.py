from rest_framework.routers import DefaultRouter

from .rule_views import BankRuleViewSet
from .views import BankStatementViewSet

router = DefaultRouter()
router.register("bank-statements", BankStatementViewSet, basename="bank-statement")
router.register("bank-rules", BankRuleViewSet, basename="bank-rule")
urlpatterns = router.urls
