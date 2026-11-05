"""Email delivery. Credentials come only from environment variables (see docs/ASSUMPTIONS.md, section D); nothing is invented.

Sending fails with `email_not_configured` when no mail server is configured, instead of pretending to send.
"""
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.utils import timezone

from apps.core.errors import DomainError

from .models import EmailLog

SMTP = "django.core.mail.backends.smtp.EmailBackend"
MAX_RECIPIENTS = 5
HOURLY_LIMIT = 30


def is_configured():
    return settings.EMAIL_BACKEND != SMTP or bool(settings.EMAIL_HOST)


def parse_recipients(raw):
    items = raw if isinstance(raw, (list, tuple)) else [x for x in str(raw or "").replace(";", ",").split(",")]
    out = []
    for x in items:
        x = str(x).strip()
        if not x:
            continue
        try:
            validate_email(x)
        except ValidationError:
            raise DomainError(f"'{x}' is not a valid email address.", "invalid_email")
        if "\n" in x or "\r" in x:
            raise DomainError("Invalid email address.", "invalid_email")
        out.append(x)
    if not out:
        raise DomainError("Enter at least one recipient (the customer or supplier has no email on file).", "no_recipient")
    if len(out) > MAX_RECIPIENTS:
        raise DomainError(f"At most {MAX_RECIPIENTS} recipients.", "too_many_recipients")
    return out


def send_pdf(org, user, *, doc_type, doc_id, to, subject, body, filename, pdf):
    if not is_configured():
        raise DomainError("Email is not configured on the server. Set EMAIL_HOST (and credentials) first; see the deployment notes.",
                          "email_not_configured")
    recipients = parse_recipients(to)
    subject = " ".join(str(subject or "").split())[:200] or filename
    if EmailLog.objects.filter(sent_by=user, created_at__gte=timezone.now() - timedelta(hours=1)).count() >= HOURLY_LIMIT:
        raise DomainError("Too many emails sent this hour. Try again later.", "email_rate_limited")
    msg = EmailMessage(subject=subject, body=str(body or "")[:5000], from_email=settings.DEFAULT_FROM_EMAIL or None, to=recipients)
    msg.attach(filename, pdf, "application/pdf")
    log = EmailLog(organization=org, doc_type=doc_type, doc_id=doc_id, to=", ".join(recipients), subject=subject, sent_by=user)
    try:
        msg.send(fail_silently=False)
    except Exception as e:  # SMTP/network errors: record, never leak details beyond a short message
        log.status, log.error = "failed", type(e).__name__
        log.save()
        raise DomainError("The email could not be sent. Check the mail server settings.", "email_failed")
    log.status = "sent"
    log.save()
    return log
