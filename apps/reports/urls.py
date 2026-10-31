from django.urls import path

from . import views

urlpatterns = [
    path("reports/trial-balance/", views.TrialBalanceView.as_view()),
    path("reports/profit-loss/", views.ProfitLossView.as_view()),
    path("reports/balance-sheet/", views.BalanceSheetView.as_view()),
    path("reports/aging/", views.AgingView.as_view()),
    path("reports/vendor/", views.VendorReportView.as_view()),
    path("reports/sales-summary/", views.SalesSummaryView.as_view()),
    path("reports/vat-summary/", views.VatSummaryView.as_view()),
]
