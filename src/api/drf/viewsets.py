"""
DRF ViewSets.
Слой агрегации доменов для Django Rest Framework.
Валидация делегируется Pydantic через to_drf_serializer.
"""
from rest_framework import viewsets

# Магия django-nova
from nova.ecosystem.drf import to_drf_serializer

# Импорты доменных моделей
from src.agents.models import Agent
from src.workflows.models import Workflow
from src.runs.models import WorkflowRun


# ==========================================
# АВТОМАТИЧЕСКАЯ ГЕНЕРАЦИЯ СЕРИАЛАЙЗЕРОВ
# ==========================================
# Под капотом to_drf_serializer читает _nova_config.pydantic_schema 
# и вешает Pydantic-валидацию на этапе validate()
AgentSerializer = to_drf_serializer(Agent)
WorkflowSerializer = to_drf_serializer(Workflow)
WorkflowRunSerializer = to_drf_serializer(WorkflowRun)


# ==========================================
# DOMAIN: AGENTS
# ==========================================
class AgentViewSet(viewsets.ModelViewSet):
    queryset = Agent.objects.all()
    serializer_class = AgentSerializer
    lookup_field = 'pk'  # Работает с UUID


# ==========================================
# DOMAIN: WORKFLOWS
# ==========================================
class WorkflowViewSet(viewsets.ModelViewSet):
    queryset = Workflow.objects.all()
    serializer_class = WorkflowSerializer
    lookup_field = 'pk'


# ==========================================
# DOMAIN: RUNS
# ==========================================
class WorkflowRunViewSet(viewsets.ModelViewSet):
    queryset = WorkflowRun.objects.all()
    serializer_class = WorkflowRunSerializer
    lookup_field = 'pk'