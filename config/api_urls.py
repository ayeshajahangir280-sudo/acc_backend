"""Single place where every module's routes are mounted under /api/v1/."""
from django.urls import include, path

urlpatterns = [
    path("", include("apps.core.urls")),
    path("", include("apps.organizations.urls")),
    path("", include("apps.parties.urls")),
    path("", include("apps.ledger.urls")),
    path("", include("apps.sales.urls")),
    path("", include("apps.purchases.urls")),
    path("", include("apps.treasury.urls")),
    path("", include("apps.vouchers.urls")),
    path("", include("apps.expenses.urls")),
    path("", include("apps.banking.urls")),
    path("", include("apps.reports.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.dispatch.urls")),
    path("", include("apps.taxes.urls")),
]
