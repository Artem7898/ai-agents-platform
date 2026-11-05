"""
DRF URL Routing.
"""

from rest_framework.routers import DefaultRouter

from src.billing.api import (
    BillingInvoiceViewSet,
    BillingPaymentViewSet,
    BillingPeriodViewSet,
    BillingPlanViewSet,
    BillingSubscriptionViewSet,
)
from src.runtimes.api import RuntimeViewSet
from src.usage.api import (
    LLMInvocationViewSet,
    LLMRateViewSet,
    OwnerQuotaViewSet,
    QuotaWindowViewSet,
)
from src.workflows.output_api import OutputSchemaViewSet

from .viewsets import AgentViewSet, WorkflowRunViewSet, WorkflowViewSet

router = DefaultRouter()

router.register(r"agents", AgentViewSet, basename="agent")
router.register(r"workflows", WorkflowViewSet, basename="workflow")
router.register(r"runs", WorkflowRunViewSet, basename="run")
router.register(r"runtimes", RuntimeViewSet, basename="runtime")
router.register(r"llm-calls", LLMInvocationViewSet, basename="llm-call")
router.register(r"llm-quotas", OwnerQuotaViewSet, basename="llm-quota")
router.register(r"llm-quota-windows", QuotaWindowViewSet, basename="llm-quota-window")
router.register(r"llm-rates", LLMRateViewSet, basename="llm-rate")
router.register(r"output-schemas", OutputSchemaViewSet, basename="output-schema")
router.register(r"billing-plans", BillingPlanViewSet, basename="billing-plan")
router.register(
    r"billing-subscriptions", BillingSubscriptionViewSet, basename="billing-subscription"
)
router.register(r"billing-periods", BillingPeriodViewSet, basename="billing-period")
router.register(r"billing-invoices", BillingInvoiceViewSet, basename="billing-invoice")
router.register(r"billing-payments", BillingPaymentViewSet, basename="billing-payment")

urlpatterns = router.urls
