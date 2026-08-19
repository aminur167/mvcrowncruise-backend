"""Payment session reuse (QA H4) + reconciliation escalation (QA H5).

- gateway_url: the checkout URL handed to the customer, so an identical
  re-request reuses that exact live session instead of minting a second
  payable one (an SSLCommerz session cannot be voided once issued).
- reconcile_attempts / last_reconcile_error / needs_manual_review: a payment
  the gateway will not resolve holds its room out of inventory, so it is
  escalated to a human instead of being retried silently forever.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0003_booking_due_reminder_sent_at_booking_refund_note_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="gateway_url",
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="payment",
            name="reconcile_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="payment",
            name="last_reconcile_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="needs_manual_review",
            field=models.BooleanField(default=False),
        ),
    ]
