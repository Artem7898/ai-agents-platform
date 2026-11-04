"""Read-only discovery of server-registered structured-output contracts."""

from rest_framework import viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import APIException
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated

from .output_schemas import OutputSchemaError, OutputSchemaRegistry


class OutputSchemaPagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 200


class OutputSchemaCatalogUnavailable(APIException):
    status_code = 503
    default_detail = "The output schema catalog is temporarily unavailable."


class OutputSchemaViewSet(viewsets.ViewSet):
    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)
    http_method_names = ("get", "head", "options")

    def list(self, request):
        paginator = OutputSchemaPagination()
        try:
            definitions = OutputSchemaRegistry.list()
        except OutputSchemaError as exc:
            raise OutputSchemaCatalogUnavailable() from exc
        page = paginator.paginate_queryset(definitions, request, view=self)
        return paginator.get_paginated_response(page)
