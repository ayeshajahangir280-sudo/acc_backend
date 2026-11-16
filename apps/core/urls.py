from django.urls import path

from .views import InvitationAcceptView, InvitationLookupView, LoginView, LogoutView, MeView

urlpatterns = [
    path("auth/login/", LoginView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/me/", MeView.as_view()),
    path("auth/invitations/lookup/", InvitationLookupView.as_view()),
    path("auth/invitations/accept/", InvitationAcceptView.as_view()),
]
