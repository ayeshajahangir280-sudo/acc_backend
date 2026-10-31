from rest_framework.routers import DefaultRouter

from .views import AdjustmentViewSet, ItemViewSet, LocationViewSet, MovementViewSet, ProductionViewSet, StockViewSet

router = DefaultRouter()
router.register("inventory/items", ItemViewSet, basename="inv-item")
router.register("inventory/locations", LocationViewSet, basename="inv-location")
router.register("inventory/stock", StockViewSet, basename="inv-stock")
router.register("inventory/movements", MovementViewSet, basename="inv-movement")
router.register("inventory/adjustments", AdjustmentViewSet, basename="inv-adjustment")
router.register("inventory/production", ProductionViewSet, basename="inv-production")
urlpatterns = router.urls
