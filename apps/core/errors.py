class DomainError(Exception):
    """A business-rule violation (unbalanced entry, over-return, cross-tenant reference...)."""

    def __init__(self, message, code="domain_error", extra=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}  # optional structured details returned to the client (e.g. per-row import errors)
