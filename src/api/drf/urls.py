"""
DRF URL Routing.
"""
from rest_framework.routers import DefaultRouter
from .viewsets import AgentViewSet, WorkflowViewSet, WorkflowRunViewSet

router = DefaultRouter()

router.register(r'agents', AgentViewSet, basename='agent')
router.register(r'workflows', WorkflowViewSet, basename='workflow')
router.register(r'runs', WorkflowRunViewSet, basename='run')

urlpatterns = router.urls