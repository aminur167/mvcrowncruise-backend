# Multiple rooms per booking: the single (room, adult_count, kid_details) that
# used to live on Booking moves to a BookingRoom child table, so one booking
# (one payment, one invoice) can hold several cabins. Existing single-room
# bookings are carried over verbatim — one BookingRoom each — before the old
# Booking columns are dropped, so no test/real data is lost.

import django.db.models.deletion
from decimal import Decimal
from django.db import migrations, models


def forwards_copy_rooms(apps, schema_editor):
    """One BookingRoom per existing Booking, copying its room + pax + priced
    subtotal/snapshot so the itemisation the customer already saw is preserved.
    is_active mirrors the booking's status (cancelled → freed room)."""
    Booking = apps.get_model("bookings", "Booking")
    BookingRoom = apps.get_model("bookings", "BookingRoom")
    for booking in Booking.objects.all():
        BookingRoom.objects.create(
            booking=booking,
            package_id=booking.package_id,
            room_id=booking.room_id,
            adult_count=booking.adult_count,
            kid_details=booking.kid_details or [],
            room_subtotal=booking.total_amount,
            price_snapshot=booking.price_snapshot or {},
            is_active=(booking.status != "cancelled"),
        )


def backwards_noop(apps, schema_editor):
    # Reverse would need to fold multi-room bookings back onto a single Booking
    # column, which is lossy by construction — this migration is forward-only.
    raise migrations.RunPython.noop


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0010_alter_invoice_pdf_file"),
        ("packages", "0006_package_package_status_end_idx_and_more"),
        ("ships", "0012_ship_contact_notify_email"),
    ]

    operations = [
        # 1. Create the child table and its FKs (all nullable-free FKs are added
        #    together so a row can be created in one go by the data step).
        migrations.CreateModel(
            name="BookingRoom",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("adult_count", models.PositiveSmallIntegerField()),
                (
                    "kid_details",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text='List of kids as [{"age": 5}, ...]. Ages drive kid pricing.',
                    ),
                ),
                (
                    "room_subtotal",
                    models.DecimalField(
                        decimal_places=2, default=Decimal("0.00"), max_digits=12
                    ),
                ),
                ("price_snapshot", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rooms",
                        to="bookings.booking",
                    ),
                ),
                (
                    "package",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="booking_rooms",
                        to="packages.package",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="booking_rooms",
                        to="ships.room",
                    ),
                ),
            ],
            options={
                "ordering": ["room__room_number"],
            },
        ),
        # 2. Copy existing bookings' single room into the new table BEFORE the
        #    old columns are dropped.
        migrations.RunPython(forwards_copy_rooms, backwards_noop),
        # 3. Now drop the old single-room columns and constraint from Booking.
        migrations.RemoveConstraint(
            model_name="booking",
            name="uniq_active_booking_per_package_room",
        ),
        migrations.RemoveField(
            model_name="booking",
            name="adult_count",
        ),
        migrations.RemoveField(
            model_name="booking",
            name="kid_details",
        ),
        migrations.RemoveField(
            model_name="booking",
            name="room",
        ),
        # 4. Finally add the partial-unique "one active hold per (package, room)"
        #    constraint — after the data is in place so it validates cleanly.
        migrations.AddConstraint(
            model_name="bookingroom",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("package", "room"),
                name="uniq_active_bookingroom_per_package_room",
            ),
        ),
    ]
