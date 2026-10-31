from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from apps.ledger.models import Account, JournalLine
from apps.organizations.models import Membership
from apps.organizations.services import create_organization
from apps.parties.models import Party

User = get_user_model()
D = Decimal


class AccountingTestCase(APITestCase):
    """Two isolated organizations (bakery, other) with one owner each, plus helpers."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-123")
        self.bob = User.objects.create_user("bob", password="pw-bob-12345")
        self.bakery = create_organization(self.alice, name="Bakery")
        self.other = create_organization(self.bob, name="Other Biz")
        self.customer = Party.objects.create(organization=self.bakery, name="Cust A", kind="customer")
        self.supplier = Party.objects.create(organization=self.bakery, name="Supp A", kind="supplier")
        self.client = self.as_user(self.alice, self.bakery)

    def as_user(self, user, org):
        c = APIClient()
        c.force_authenticate(user)
        c.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
        return c

    def acct(self, code, org=None):
        return Account.objects.get(organization=org or self.bakery, system_code=code)

    def balance(self, code, org=None):
        """Debit-minus-credit for a system account."""
        a = self.acct(code, org)
        from django.db.models import Sum

        agg = JournalLine.objects.filter(account=a).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or D(0)) - (agg["c"] or D(0))

    def trial_balance_ok(self, org=None):
        from django.db.models import Sum

        agg = JournalLine.objects.filter(organization=org or self.bakery).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (agg["d"] or D(0)) == (agg["c"] or D(0))

    def sale(self, qty="2", price="50.00", tax="10", **extra):
        body = {"party": self.customer.id, "date": "2026-01-10",
                "items": [{"description": "Bread", "quantity": qty, "unit_price": price, "tax_rate": tax}], **extra}
        return self.client.post("/api/v1/sales/invoices/", body, format="json")
