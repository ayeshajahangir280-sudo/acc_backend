from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .errors import DomainError


def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        return Response({"detail": exc.message, "code": exc.code, **getattr(exc, "extra", {})}, status=400)
    if isinstance(exc, DjangoValidationError):
        return Response({"detail": "; ".join(exc.messages), "code": "validation_error"}, status=400)
    return exception_handler(exc, context)
