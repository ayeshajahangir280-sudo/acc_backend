from rest_framework.routers import DefaultRouter

from .views import PurchaseInvoiceViewSet

router = DefaultRouter()
router.register("purchases/invoices", PurchaseInvoiceViewSet, basename="purchase-invoice")
urlpatterns = router.urls

