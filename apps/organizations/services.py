from django.db import transaction

from .models import Membership, Organization


@transaction.atomic
def create_organization(user, **fields):
    from apps.ledger.services import seed_chart_of_accounts

    org = Organization.objects.create(**fields)
    Membership.objects.create(user=user, organization=org, role=Membership.Role.OWNER)
    seed_chart_of_accounts(org)
    return org
