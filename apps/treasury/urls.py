from rest_framework.routers import DefaultRouter

from .views import PaymentViewSet, ReceiptViewSet

router = DefaultRouter()
router.register("receipts", ReceiptViewSet, basename="receipt")
router.register("payments", PaymentViewSet, basename="payment")
urlpatterns = router.urls
