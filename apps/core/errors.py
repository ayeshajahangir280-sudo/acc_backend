class DomainError(Exception):
    """A business-rule violation (unbalanced entry, over-return, cross-tenant reference...)."""

    def __init__(self, message, code="domain_error"):
        super().__init__(message)
        self.message = message
        self.code = code
