from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.ships.models import Ship
from apps.testing import ThrottlelessTestMixin

from .models import ContactMessage

User = get_user_model()


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CONTACT_NOTIFY_EMAIL="default-inbox@example.com",
)
class ContactMessagePublicTests(ThrottlelessTestMixin, APITestCase):
    def test_create_saves_and_emails_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/contact-messages/",
                {
                    "name": "Ada Customer",
                    "email": "ada@example.com",
                    "phone": "01700000000",
                    "message": "Interested in the December sailing.",
                    "guests": 4,
                },
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(ContactMessage.objects.count(), 1)
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.status, ContactMessage.Status.NEW)

        # Notification email fired to the resolved recipient, reply-to customer.
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Ada Customer", email.subject)
        self.assertEqual(email.reply_to, ["ada@example.com"])
        self.assertIn("Interested in the December sailing.", email.body)
        # A branded HTML alternative is attached alongside the plain-text body.
        self.assertEqual(len(email.alternatives), 1)
        html, mimetype = email.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("WEBSITE CONTACT INQUIRY", html)
        self.assertIn("Ada Customer", html)

    def test_ship_override_wins_over_default_recipient(self):
        ship = Ship.objects.order_by("id").first()
        ship.contact_notify_email = "ship-inbox@example.com"
        ship.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/contact-messages/",
                {"name": "B", "phone": "0170", "message": "hi"},
                format="json",
            )
        self.assertEqual(mail.outbox[0].to, ["ship-inbox@example.com"])

    def test_inquiry_type_saved_and_defaults_to_general(self):
        # Explicit type is stored and shown in the notification subject.
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                "/api/contact-messages/",
                {
                    "name": "Group Lead",
                    "phone": "0170",
                    "message": "Chartering the whole ship for a wedding.",
                    "inquiry_type": "charter",
                },
                format="json",
            )
        msg = ContactMessage.objects.get()
        self.assertEqual(msg.inquiry_type, "charter")
        self.assertIn("full ship charter", mail.outbox[0].subject.lower())

        # Omitting the field falls back to the model default.
        ContactMessage.objects.all().delete()
        self.client.post(
            "/api/contact-messages/",
            {"name": "Walk-in", "phone": "0170", "message": "hi"},
            format="json",
        )
        self.assertEqual(ContactMessage.objects.get().inquiry_type, "general")

    def test_invalid_inquiry_type_rejected(self):
        response = self.client.post(
            "/api/contact-messages/",
            {
                "name": "X",
                "phone": "0170",
                "message": "hi",
                "inquiry_type": "not-a-real-type",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_email_or_phone(self):
        response = self.client.post(
            "/api/contact-messages/",
            {"name": "No Contact", "message": "hello"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_response_never_exposes_status_field_control(self):
        # Client cannot pre-set status to something other than NEW.
        self.client.post(
            "/api/contact-messages/",
            {
                "name": "C",
                "phone": "0170",
                "message": "hi",
                "status": "archived",
            },
            format="json",
        )
        self.assertEqual(ContactMessage.objects.get().status, "new")


class StaffContactMessageTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client.force_authenticate(self.staff)
        self.msg = ContactMessage.objects.create(
            name="Lead", phone="0170", message="hi"
        )

    def test_list_requires_staff(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.get("/api/staff/contact-messages/").status_code, 401
        )

    def test_staff_can_list_and_mark_read(self):
        listing = self.client.get("/api/staff/contact-messages/")
        self.assertEqual(listing.status_code, 200)

        patch = self.client.patch(
            f"/api/staff/contact-messages/{self.msg.id}/",
            {"status": "read"},
            format="json",
        )
        self.assertEqual(patch.status_code, 200)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.status, "read")

    def test_status_filter(self):
        ContactMessage.objects.create(
            name="Archived", phone="0170", message="x", status="archived"
        )
        resp = self.client.get("/api/staff/contact-messages/?status=archived")
        names = [m["name"] for m in resp.data["results"]] if isinstance(
            resp.data, dict
        ) else [m["name"] for m in resp.data]
        self.assertEqual(names, ["Archived"])

    def test_staff_cannot_edit_customer_fields(self):
        self.client.patch(
            f"/api/staff/contact-messages/{self.msg.id}/",
            {"name": "Hacked"},
            format="json",
        )
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.name, "Lead")

    def test_staff_can_delete(self):
        resp = self.client.delete(
            f"/api/staff/contact-messages/{self.msg.id}/"
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(ContactMessage.objects.count(), 0)
