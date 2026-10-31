"""Idempotent creation: a retried or duplicated request must return the original record, never an error or a second copy.

Services look up an existing record by (organization, idempotency_key) before creating. Two requests that arrive at the
same instant can both miss that lookup; the database's unique constraint then rejects the slower insert. The decorator
below turns that rejection into "run once more": the failed attempt has already been rolled back completely (document,
journal lines, document number), and the second run finds the winner and returns it.
"""
from functools import wraps

from django.db import IntegrityError


def find_existing(model, org, key):
    if not key:
        return None
    return model.objects.filter(organization=org, idempotency_key=key).first()


def retry_on_key_conflict(fn):
    """Apply OUTSIDE @transaction.atomic so the failed attempt is rolled back before the retry.

    Only retries when an idempotency key was supplied. Any other database error, or a second failure, propagates.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntegrityError:
            if not kwargs.get("idempotency_key"):
                raise
            return fn(*args, **kwargs)

    return wrapper
