from rest_framework.routers import DefaultRouter

from .views import TaxRateViewSet

router = DefaultRouter()
router.register("tax-rates", TaxRateViewSet, basename="tax-rate")
urlpatterns = router.urls
