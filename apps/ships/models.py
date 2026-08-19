from django.db import models, transaction
from django.utils.text import slugify


class Ship(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100, unique=True)
    layout_image = models.ImageField(upload_to="ships/layouts/", blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    #: Helpline numbers printed on this ship's guide report & customer invoices,
    #: comma-separated. Editable from the staff dashboard; when blank the PDFs
    #: fall back to settings.AUTHORITY_PHONES. Per-ship so each ship can carry
    #: its own contact numbers.
    authority_phones = models.CharField(
        max_length=255,
        blank=True,
        help_text="Helpline numbers for the report/invoice header, comma-separated.",
    )
    #: Where this ship's public /contact form submissions are emailed. Editable
    #: from the staff dashboard; when blank, falls back to
    #: settings.CONTACT_NOTIFY_EMAIL.
    contact_notify_email = models.EmailField(
        blank=True,
        help_text="Inbox for website contact-form messages. Blank uses the system default.",
    )

    class GuideReportDensity(models.TextChoices):
        COMPACT = "compact", "Compact"  # smaller type, more rooms per page
        NORMAL = "normal", "Normal"
        LARGE = "large", "Large"  # bigger type, easier to read, may span pages

    #: How dense the guide collection report PDF is. Per-ship so a busy sailing
    #: can pack more rooms on one page (compact) while another prints large,
    #: easy-to-read type; the guide reads it on the ship, so legibility vs.
    #: page-count is the ship operator's call.
    guide_report_density = models.CharField(
        max_length=10,
        choices=GuideReportDensity.choices,
        default=GuideReportDensity.NORMAL,
        help_text="Guide report PDF text size / rows-per-page.",
    )

    # ---- Cancellation / refund policy knobs ------------------------------
    # Business policy, so data rather than constants — and per-ship, since a
    # second ship may run a different operation. The charge SCHEDULE itself is
    # its own table (refunds.CancellationRule); these are the scalars around it.
    group_min_pax = models.PositiveSmallIntegerField(
        default=10,
        help_text=(
            "Pax count at which the dashboard suggests marking a booking as a "
            "group. A suggestion only — booking_type is always set by a human."
        ),
    )
    refund_sla_days = models.PositiveSmallIntegerField(
        default=14,
        help_text=(
            "Working days quoted to the customer for a refund payout. Drives "
            "the promise in the cancellation email and the overdue-refund alert."
        ),
    )
    refund_claim_window_days = models.PositiveSmallIntegerField(
        default=30,
        help_text=(
            "Days after a sailing ends within which staff may raise a refund "
            "normally. Later claims need an explicit override, so a closed "
            "accounting month is never reopened by accident."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @property
    def total_rooms(self):
        return self.rooms.count()

    @property
    def authority_phone_list(self):
        """The ship's helpline numbers as a clean list, falling back to the
        system default (settings.AUTHORITY_PHONES) when none are set — so a
        report/invoice never prints an empty helpline line."""
        from django.conf import settings

        raw = self.authority_phones or getattr(
            settings, "AUTHORITY_PHONES", ""
        )
        return [n.strip() for n in raw.split(",") if n.strip()]

    @property
    def contact_notify_recipient(self):
        """The email address website contact-form messages for this ship go to,
        falling back to the system default (settings.CONTACT_NOTIFY_EMAIL) when
        the ship has no override set."""
        from django.conf import settings

        return self.contact_notify_email or getattr(
            settings, "CONTACT_NOTIFY_EMAIL", ""
        )


class RoomType(models.Model):
    name = models.CharField(max_length=50, unique=True)
    max_adults = models.PositiveSmallIntegerField()
    max_kids = models.PositiveSmallIntegerField()
    base_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.name} (max {self.max_adults} adults, {self.max_kids} kids)"


class Room(models.Model):
    ship = models.ForeignKey(Ship, on_delete=models.PROTECT, related_name="rooms")
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, related_name="rooms"
    )
    room_number = models.CharField(max_length=20)
    floor_number = models.IntegerField(
        null=True, blank=True, help_text="1 = 1st floor (200-series), 2 = 2nd floor (300-series)."
    )

    class Meta:
        ordering = ["room_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["ship", "room_number"], name="uniq_room_number_per_ship"
            ),
        ]

    def __str__(self):
        return f"{self.ship.name} — Room {self.room_number}"


def room_image_path(room_image, filename):
    """rooms/<ship_id>/<room_number>/<original name>. Keyed by ship so two
    ships' identically numbered rooms never share a folder."""
    room = room_image.room
    return f"rooms/{room.ship_id}/{room.room_number}/{filename}"


class RoomImage(models.Model):
    """A gallery photo of a room, uploaded from the admin. Rooms carry any
    number of images; today they surface in the room API payloads, and later
    features (galleries, sliders, previews) read from this same table."""

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=room_image_path)
    caption = models.CharField(max_length=150, blank=True)
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Lower numbers show first."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.room} — image {self.pk}"


class Cabin(models.Model):
    """A marketing cabin category shown on the public /cabins pages
    (Premier Balcony Suite, Panorama View Cabin, …) — content is fully
    staff-managed from the dashboard.

    This is showcase content only: prices and availability are never part of
    it (pricing lives on RoomType/packages and is deliberately not exposed on
    the cabins pages). `room_type` is used purely to display occupancy limits
    ("3 Adults + 1 Kids") from the single source of truth.
    """

    ship = models.ForeignKey(Ship, on_delete=models.CASCADE, related_name="cabins")
    room_type = models.ForeignKey(
        RoomType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cabins",
        help_text="Used to show occupancy (max adults/kids) on the cabin card.",
    )
    slug = models.SlugField(
        max_length=80,
        unique=True,
        blank=True,
        help_text="URL id, e.g. premier-balcony-suite. Auto-generated from the name when left blank.",
    )
    name = models.CharField(max_length=100)
    tagline = models.CharField(
        max_length=200,
        blank=True,
        help_text="One-line teaser under the name on the detail page.",
    )
    description = models.TextField(
        blank=True, help_text="Long 'About this cabin' text on the detail page."
    )
    size_label = models.CharField(
        max_length=30, blank=True, help_text='Shown on the card badge, e.g. "32 m²".'
    )
    #: List of feature strings; first 4 show on the card, all on the detail page.
    features = models.JSONField(default=list, blank=True)
    #: List of {"label": ..., "value": ...} rows for the detail page spec table.
    amenities = models.JSONField(default=list, blank=True)
    #: List of {"title": ..., "desc": ...} blocks for the detail page.
    highlights = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to hide from the website without deleting."
    )
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Lower numbers show first."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.ship.name} — {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:70] or "cabin"
            slug = base
            counter = 2
            while Cabin.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def occupancy_label(self):
        """Display string from the linked room type ("3 Adults + 1 Kids"),
        empty when no room type is linked."""
        if not self.room_type:
            return ""
        label = f"{self.room_type.max_adults} Adults"
        if self.room_type.max_kids:
            label += f" + {self.room_type.max_kids} Kids"
        return label

    @property
    def main_image(self):
        """The image staff picked for the card (is_main), falling back to the
        first gallery image so a cabin never renders blank just because no
        main was chosen yet."""
        images = list(self.images.all())
        return images[0] if images else None


def cabin_image_path(cabin_image, filename):
    """cabins/<ship_id>/<slug>/<original name> — keyed by ship so two ships'
    identically named cabins never share a folder."""
    cabin = cabin_image.cabin
    return f"cabins/{cabin.ship_id}/{cabin.slug}/{filename}"


class CabinImage(models.Model):
    """A gallery photo of a showcase cabin. The one flagged `is_main` is the
    card/hero image; the rest appear in the detail page gallery."""

    cabin = models.ForeignKey(Cabin, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=cabin_image_path)
    caption = models.CharField(max_length=150, blank=True)
    is_main = models.BooleanField(
        default=False, help_text="Shown on the cabin card. Only one per cabin."
    )
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Lower numbers show first."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Main image first, so `cabin.images.all()[0]` is always the card image.
        ordering = ["-is_main", "sort_order", "id"]

    def __str__(self):
        return f"{self.cabin} — image {self.pk}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self.is_main:
                # One main per cabin, enforced at the model layer so every
                # writer (API, admin, shell) gets the same behavior.
                CabinImage.objects.filter(cabin=self.cabin, is_main=True).exclude(
                    pk=self.pk
                ).update(is_main=False)
            super().save(*args, **kwargs)


def gallery_image_path(gallery_image, filename):
    """gallery/<ship_id>/<original name> — keyed by ship so each ship's
    public gallery keeps its own folder."""
    return f"gallery/{gallery_image.ship_id}/{filename}"


class GalleryImage(models.Model):
    """A photo on the public /gallery page — fully staff-managed from the
    dashboard (upload, caption text, ordering, hide/show)."""

    ship = models.ForeignKey(
        Ship, on_delete=models.CASCADE, related_name="gallery_images"
    )
    image = models.ImageField(upload_to=gallery_image_path)
    caption = models.CharField(
        max_length=200, blank=True, help_text="Short text shown on the photo."
    )
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to hide from the website without deleting."
    )
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Lower numbers show first."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.ship.name} — gallery image {self.pk}"


class FoodMenuItem(models.Model):
    """A dish the chef may serve on a given day/meal. Rows are a selection
    pool, not a fixed daily assignment — the chef picks from the active
    items for that (ship, day, meal_type) on the day."""

    class Day(models.TextChoices):
        DAY_1 = "day_1", "Day 1"
        DAY_2 = "day_2", "Day 2"
        DAY_3 = "day_3", "Day 3"

    class MealType(models.TextChoices):
        # Declaration order is the serving order of the day, and the serializer
        # emits meals in exactly this order. Days 1 and 2 have two snack
        # sittings (one before lunch, one after), so they are separate values
        # rather than one merged "snacks" bucket.
        BREAKFAST = "breakfast", "Breakfast"
        SNACKS = "snacks", "Snacks"
        LUNCH = "lunch", "Lunch"
        EVENING_SNACKS = "evening_snacks", "Evening Snacks"
        DINNER = "dinner", "Dinner"

    ship = models.ForeignKey(
        Ship, on_delete=models.CASCADE, related_name="food_menu_items"
    )
    day = models.CharField(max_length=10, choices=Day.choices)
    meal_type = models.CharField(max_length=20, choices=MealType.choices)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to hide without deleting."
    )
    order = models.PositiveSmallIntegerField(
        default=0, help_text="Display order within the same day/meal."
    )

    class Meta:
        ordering = ["ship", "day", "meal_type", "order", "id"]

    def __str__(self):
        return f"{self.ship.name} — {self.get_day_display()} {self.get_meal_type_display()}: {self.name}"
