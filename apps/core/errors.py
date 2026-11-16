class DomainError(Exception):
    """A business-rule violation (unbalanced entry, over-return, cross-tenant reference...)."""

    status = 400

    def __init__(self, message, code="domain_error", extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}  # optional structured details returned to the client (e.g. per-row import errors)


class Forbidden(DomainError):
    """The caller is not allowed to do this (returned as HTTP 403 with the same {detail, code} body)."""

    status = 403
