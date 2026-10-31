from rest_framework.routers import DefaultRouter

from .views import VanLoadViewSet, VehicleViewSet

router = DefaultRouter()
router.register("dispatch/vehicles", VehicleViewSet, basename="vehicle")
router.register("dispatch/loads", VanLoadViewSet, basename="van-load")
urlpatterns = router.urls
