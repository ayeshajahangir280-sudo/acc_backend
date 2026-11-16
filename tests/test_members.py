"""Organization membership management: invitations, adding existing users, deactivation, removal and guard rails."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.errors import DomainError
from apps.organizations.models import Invitation, Membership

from .base import AccountingTestCase

User = get_user_model()
MEMBERS, INVITES = "/api/v1/members/", "/api/v1/invitations/"
LOOKUP, ACCEPT = "/api/v1/auth/invitations/lookup/", "/api/v1/auth/invitations/accept/"


def token_of(resp):
    return resp.data["invite_path"].split("token=", 1)[1]


class MemberTests(AccountingTestCase):
    def setUp(self):
        super().setUp()
        self.carol = User.objects.create_user("carol", email="carol@example.com", password="pw-carol-12345")
        self.dave = User.objects.create_user("dave", email="dave@example.com", password="pw-dave-123456")
        self.m_dave = Membership.objects.create(user=self.dave, organization=self.bakery, role="accountant")
        self.anon = APIClient()

    def member(self, user):
        return Membership.objects.get(user=user, organization=self.bakery)

    # --- existing data -------------------------------------------------------------------------------
    def test_existing_memberships_are_active_and_unchanged(self):
        self.assertTrue(all(Membership.objects.values_list("is_active", flat=True)))
        self.assertEqual(self.client.get("/api/v1/accounts/").status_code, 200)
        rows = self.client.get(MEMBERS).data
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual({r["username"] for r in rows}, {"alice", "dave"})
        self.assertTrue(next(r for r in rows if r["username"] == "alice")["is_self"])

    # --- adding people -------------------------------------------------------------------------------
    def test_invite_existing_user_adds_them_directly(self):
        r = self.client.post(INVITES, {"email": "CAROL@example.com", "role": "viewer"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["kind"], "member")
        self.assertEqual(self.as_user(self.carol, self.bakery).get("/api/v1/accounts/").status_code, 200)
        again = self.client.post(INVITES, {"email": "carol@example.com", "role": "viewer"}, format="json")
        self.assertEqual(again.data["code"], "already_member")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.example.com")
    def test_invitation_flow_new_person(self):
        r = self.client.post(INVITES, {"email": "erin@example.com", "role": "accountant"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["kind"], "invitation")
        self.assertTrue(r.data["invite_url"].startswith("https://app.example.com/accept-invite#token="))
        self.assertTrue(r.data["emailed"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(r.data["invite_url"], mail.outbox[0].body)
        raw = token_of(r)
        inv = Invitation.objects.get()
        self.assertNotEqual(inv.token_hash, raw)  # only the hash is stored
        look = self.anon.post(LOOKUP, {"token": raw}, format="json")
        self.assertEqual((look.status_code, look.data["organization_name"], look.data["existing_account"]), (200, "Bakery", False))
        weak = self.anon.post(ACCEPT, {"token": raw, "username": "erin", "password": "123"}, format="json")
        self.assertEqual(weak.data["code"], "weak_password")
        taken = self.anon.post(ACCEPT, {"token": raw, "username": "Carol", "password": "a-Strong-pass-77"}, format="json")
        self.assertEqual(taken.data["code"], "username_taken")
        ok = self.anon.post(ACCEPT, {"token": raw, "username": "erin", "password": "a-Strong-pass-77"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        erin = User.objects.get(username="erin")
        self.assertEqual((erin.email, self.member(erin).role), ("erin@example.com", "accountant"))
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {ok.data['token']}", HTTP_X_ORGANIZATION_ID=str(self.bakery.id))
        self.assertEqual(c.get("/api/v1/accounts/").status_code, 200)
        # a used link cannot be reused
        self.assertEqual(self.anon.post(ACCEPT, {"token": raw, "username": "erin2", "password": "a-Strong-pass-77"}, format="json").status_code, 400)
        self.assertEqual(self.anon.post(LOOKUP, {"token": raw}, format="json").status_code, 404)

    def test_link_always_returned_without_mail(self):
        r = self.client.post(INVITES, {"email": "frank@example.com", "role": "viewer"}, format="json")
        self.assertFalse(r.data["emailed"])
        self.assertTrue(r.data["invite_path"].startswith("/accept-invite#token="))

    def test_expired_cancelled_and_resent_links(self):
        r = self.client.post(INVITES, {"email": "gina@example.com", "role": "viewer"}, format="json")
        inv_id, old = r.data["invitation"]["id"], token_of(r)
        new = token_of(self.client.post(f"{INVITES}{inv_id}/resend/"))
        self.assertEqual(self.anon.post(LOOKUP, {"token": old}, format="json").status_code, 404)  # old link dead
        self.assertEqual(self.anon.post(LOOKUP, {"token": new}, format="json").status_code, 200)
        Invitation.objects.filter(pk=inv_id).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.anon.post(ACCEPT, {"token": new, "username": "gina", "password": "a-Strong-pass-77"}, format="json").status_code, 400)
        r2 = self.client.post(INVITES, {"email": "hal@example.com", "role": "viewer"}, format="json")
        self.client.post(f"{INVITES}{r2.data['invitation']['id']}/cancel/")
        self.assertEqual(self.anon.post(ACCEPT, {"token": token_of(r2), "username": "hal", "password": "a-Strong-pass-77"}, format="json").status_code, 400)
        self.assertFalse(User.objects.filter(username__in=["gina", "hal"]).exists())

    def test_existing_account_must_sign_in_to_accept(self):
        r = self.client.post(INVITES, {"email": "ivy@example.com", "role": "viewer"}, format="json")
        ivy = User.objects.create_user("ivy", email="ivy@example.com", password="pw-ivy-1234567")  # registered after the invite
        raw = token_of(r)
        self.assertTrue(self.anon.post(LOOKUP, {"token": raw}, format="json").data["existing_account"])
        bad = self.anon.post(ACCEPT, {"token": raw, "username": "ivy", "password": "wrong-password"}, format="json")
        self.assertEqual(bad.data["code"], "invalid_credentials")
        ok = self.anon.post(ACCEPT, {"token": raw, "username": "ivy@example.com", "password": "pw-ivy-1234567"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(self.member(ivy).role, "viewer")

    # --- deactivate / remove -------------------------------------------------------------------------
    def test_deactivated_member_loses_access_immediately(self):
        dave = self.as_user(self.dave, self.bakery)
        self.assertEqual(dave.get("/api/v1/accounts/").status_code, 200)
        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"is_active": False}, format="json").status_code, 200)
        self.assertEqual(dave.get("/api/v1/accounts/").status_code, 403)
        self.assertEqual(dave.get("/api/v1/sales/invoices/").status_code, 403)
        self.assertEqual(dave.get("/api/v1/organizations/").data["count"] if isinstance(dave.get("/api/v1/organizations/").data, dict)
                         else len(dave.get("/api/v1/organizations/").data), 0)
        self.assertEqual(dave.get(f"/api/v1/organizations/{self.bakery.id}/").status_code, 404)
        self.assertEqual(dave.get("/api/v1/auth/me/").data["memberships"], [])
        self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"is_active": True}, format="json")
        self.assertEqual(dave.get("/api/v1/accounts/").status_code, 200)

    def test_remove_keeps_user_and_records(self):
        self.m_dave.role = "admin"  # admins post without a branch; accountants must pick one of their branches
        self.m_dave.save()
        dave = self.as_user(self.dave, self.bakery)
        inv = dave.post("/api/v1/sales/invoices/", {"party": self.customer.id, "date": "2026-01-10",
                        "items": [{"description": "x", "quantity": "1", "unit_price": "10"}]}, format="json")
        self.assertEqual(inv.status_code, 201, inv.content)
        self.assertEqual(self.client.delete(f"{MEMBERS}{self.m_dave.id}/").status_code, 204)
        self.assertTrue(User.objects.filter(pk=self.dave.pk).exists())
        self.assertEqual(self.client.get(f"/api/v1/sales/invoices/{inv.data['id']}/").status_code, 200)
        self.assertEqual(dave.get("/api/v1/accounts/").status_code, 403)

    # --- guard rails ---------------------------------------------------------------------------------
    def test_no_self_change_and_last_owner_protected(self):
        me = self.member(self.alice)
        for body in ({"role": "viewer"}, {"is_active": False}):
            self.assertEqual(self.client.patch(f"{MEMBERS}{me.id}/", body, format="json").data["code"], "self_change")
        self.assertEqual(self.client.delete(f"{MEMBERS}{me.id}/").data["code"], "self_change")
        # a second owner may not demote the only other... but with two owners demoting one is fine, the last one is not
        self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "owner"}, format="json")
        dave = self.as_user(self.dave, self.bakery)
        self.assertEqual(dave.patch(f"{MEMBERS}{me.id}/", {"role": "admin"}, format="json").status_code, 200)
        r = self.as_user(self.alice, self.bakery).patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "viewer"}, format="json")
        self.assertEqual(r.status_code, 403)  # alice is admin now: cannot touch an owner
        from apps.organizations import members
        with self.assertRaises(Exception) as cm:
            members.update_member(self.bakery, self.member(self.carol) if False else Membership(user=self.carol, organization=self.bakery, role="owner", is_active=True),
                                  self.member(self.dave), role="viewer")
        self.assertIn("last_owner", str(getattr(cm.exception, "code", cm.exception)))

    def test_last_active_owner_cannot_be_removed_deactivated_or_demoted(self):
        owner = self.member(self.alice)
        self.assertEqual(self.client.delete(f"{MEMBERS}{owner.id}/").data["code"], "self_change")
        for body in ({"is_active": False}, {"role": "admin"}):
            self.assertEqual(self.client.patch(f"{MEMBERS}{owner.id}/", body, format="json").data["code"], "self_change")

        actor = Membership(user=self.carol, organization=self.bakery, role=Membership.Role.OWNER, is_active=True)
        from apps.organizations import members

        actions = (
            lambda: members.remove_member(self.bakery, actor, owner),
            lambda: members.update_member(self.bakery, actor, owner, is_active=False),
            lambda: members.update_member(self.bakery, actor, owner, role=Membership.Role.ADMIN),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(DomainError) as cm:
                    action()
                self.assertEqual(cm.exception.code, "last_owner")

    def test_another_active_owner_can_be_demoted_deactivated_and_removed(self):
        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "owner"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "admin"}, format="json").status_code, 200)
        self.assertEqual(self.member(self.alice).role, Membership.Role.OWNER)

        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "owner"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"is_active": False}, format="json").status_code, 200)
        self.assertTrue(self.member(self.alice).is_active)
        self.assertEqual(self.member(self.alice).role, Membership.Role.OWNER)

        self.assertEqual(self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"is_active": True}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(f"{MEMBERS}{self.m_dave.id}/").status_code, 204)
        self.assertTrue(self.member(self.alice).is_active)
        self.assertEqual(self.member(self.alice).role, Membership.Role.OWNER)

    def test_admin_cannot_create_or_change_owners_and_non_admins_cannot_manage(self):
        self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "admin"}, format="json")
        dave = self.as_user(self.dave, self.bakery)
        self.assertEqual(dave.post(INVITES, {"email": "x@example.com", "role": "owner"}, format="json").data["code"], "owner_only")
        self.assertEqual(dave.patch(f"{MEMBERS}{self.member(self.alice).id}/", {"is_active": False}, format="json").status_code, 403)
        self.assertEqual(dave.post(INVITES, {"email": "x@example.com", "role": "viewer"}, format="json").status_code, 201)
        self.client.patch(f"{MEMBERS}{self.m_dave.id}/", {"role": "accountant"}, format="json")
        self.assertEqual(dave.post(INVITES, {"email": "y@example.com", "role": "viewer"}, format="json").status_code, 403)
        self.assertEqual(dave.get(INVITES).status_code, 403)
        self.assertEqual(dave.patch(f"{MEMBERS}{self.member(self.alice).id}/", {"role": "viewer"}, format="json").status_code, 403)

    def test_organization_isolation(self):
        bob = self.as_user(self.bob, self.other)
        self.assertEqual(bob.patch(f"{MEMBERS}{self.m_dave.id}/", {"is_active": False}, format="json").status_code, 404)
        self.assertEqual(bob.delete(f"{MEMBERS}{self.m_dave.id}/").status_code, 404)
        r = self.client.post(INVITES, {"email": "z@example.com", "role": "viewer"}, format="json")
        self.assertEqual(bob.post(f"{INVITES}{r.data['invitation']['id']}/cancel/").status_code, 404)
        self.assertTrue(self.member(self.dave).is_active)
