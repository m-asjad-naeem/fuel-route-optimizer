from django.urls import path

from routes.api.views import RoutePlanView

urlpatterns = [
    path("v1/routes/plan", RoutePlanView.as_view(), name="route-plan"),
]
