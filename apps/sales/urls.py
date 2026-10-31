from rest_framework.routers import DefaultRouter

from .views import SalesInvoiceViewSet

router = DefaultRouter()
router.register("sales/invoices", SalesInvoiceViewSet, basename="sales-invoice")
urlpatterns = router.urls
