from django.urls import path

from . import views

app_name = "triage"
urlpatterns = [path("sink/", views.sink, name="sink")]
