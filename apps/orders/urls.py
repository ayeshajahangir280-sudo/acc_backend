from rest_framework.routers import DefaultRouter

from .views import PurchaseOrderViewSet, SalesOrderViewSet

router = DefaultRouter()
router.register("sales/orders", SalesOrderViewSet, basename="sales-order")
router.register("purchases/orders", PurchaseOrderViewSet, basename="purchase-order")
urlpatterns = router.urls
