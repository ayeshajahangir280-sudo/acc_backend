from rest_framework.routers import DefaultRouter

from .views import RecurringExpenseViewSet, RecurringViewSet

router = DefaultRouter()
router.register("recurring", RecurringViewSet, basename="recurring")
router.register("recurring-expenses", RecurringExpenseViewSet, basename="recurring-expense")
urlpatterns = router.urls
