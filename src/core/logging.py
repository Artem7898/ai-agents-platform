import logging
import structlog
from opentelemetry import trace

def add_otel_context(logger, method_name, event_dict):
    """Injects OTel trace/span IDs into every log record."""
    span = trace.get_current_span()
    if span.is_recording():
        ctx = span.get_span_context()
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        add_otel_context,
        structlog.processors.TimeStamper(fmt="iso"), # ISO-8601
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(), # JSON output for Loki/Datadog
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)