from django.db import models


class ContactMessage(models.Model):
    """An inquiry submitted from the public /contact form.

    Stored so staff can work the enquiry queue from the dashboard, and emailed
    to the notification address on creation (see the public create view). This
    is a lead/enquiry record — it never touches booking, room or payment state.
    """

    class Status(models.TextChoices):
        NEW = "new", "New"
        READ = "read", "Read"
        ARCHIVED = "archived", "Archived"

    class InquiryType(models.TextChoices):
        GENERAL = "general", "General inquiry"
        FAMILY = "family", "Family trip"
        CORPORATE = "corporate", "Corporate / group trip"
        CHARTER = "charter", "Full ship charter"

    name = models.CharField(max_length=120)
    inquiry_type = models.CharField(
        max_length=20,
        choices=InquiryType.choices,
        default=InquiryType.GENERAL,
        help_text="What kind of trip the customer is asking about.",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    #: Free-text; the fields below are optional context the form collects.
    message = models.TextField(max_length=2000)
    departure_date = models.DateField(null=True, blank=True)
    guests = models.PositiveSmallIntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.NEW
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"{self.name} — {self.created_at:%Y-%m-%d}"
