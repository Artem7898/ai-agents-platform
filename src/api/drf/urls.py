"""
DRF URL Routing.
"""

from rest_framework.routers import DefaultRouter

from src.runtimes.api import RuntimeViewSet

from .viewsets import AgentViewSet, WorkflowRunViewSet, WorkflowViewSet

router = DefaultRouter()

router.register(r"agents", AgentViewSet, basename="agent")
router.register(r"workflows", WorkflowViewSet, basename="workflow")
router.register(r"runs", WorkflowRunViewSet, basename="run")
router.register(r"runtimes", RuntimeViewSet, basename="runtime")

urlpatterns = router.urls
