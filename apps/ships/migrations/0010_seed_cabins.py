"""Seed the three showcase cabins with the marketing copy that previously
lived hardcoded in the frontend (src/data/cabins.ts), so the public /cabins
pages have real content the moment the dashboard takes over. Images are NOT
seeded — staff upload those from the dashboard (frontend falls back to a
bundled placeholder until then).

Idempotent: cabins are matched by slug and updated in place.
"""

from django.db import migrations

CABINS = [
    {
        "slug": "premier-balcony-suite",
        "room_type_name": "3-Person Room",
        "name": "Premier Balcony Suite",
        "tagline": "Floor-to-ceiling glass, private deck, river at your doorstep.",
        "size_label": "32 m²",
        "sort_order": 1,
        "description": (
            "The Premier Balcony Suite is the pinnacle of river living. Step "
            "through wide glass doors onto your private balcony and wake to the "
            "sounds of the Sundarbans. Wood-panel interiors, ambient lighting, "
            "and bespoke furnishings create an atmosphere of quiet luxury — "
            "while every meal, excursion, and naturalist tour is taken care of."
        ),
        "features": [
            "Private river-facing balcony",
            "King-size bed with Egyptian cotton",
            "Floor-to-ceiling glass sliding doors",
            "En-suite marble bathroom with rain shower",
            "Bespoke wood-panel interior",
            "Dedicated cabin steward",
        ],
        "amenities": [
            {"label": "Size", "value": "32 m²"},
            {"label": "Bed type", "value": "King"},
            {"label": "Bathroom", "value": "En-suite marble"},
            {"label": "View", "value": "River-facing"},
            {"label": "Internet", "value": "Starlink Wi-Fi"},
            {"label": "Climate", "value": "Inverter AC"},
            {"label": "Mini bar", "value": "Complimentary"},
        ],
        "highlights": [
            {
                "title": "Private Balcony",
                "desc": "Step outside at dawn and watch mist rise over the delta from your exclusive riverside terrace.",
            },
            {
                "title": "Marble En-Suite",
                "desc": "A full marble bathroom with rainfall shower, premium toiletries, and plush robes.",
            },
            {
                "title": "Dedicated Steward",
                "desc": "Your personal cabin steward is on call for turndown service, in-cabin dining, and any request.",
            },
        ],
    },
    {
        "slug": "panorama-view-cabin",
        "room_type_name": "2-Person Room",
        "name": "Panorama View Cabin",
        "tagline": "Sweeping river views through floor-spanning panoramic windows.",
        "size_label": "26 m²",
        "sort_order": 2,
        "description": (
            "The Panorama View Cabin frames the Sundarbans like a living "
            "painting. Oversized panoramic windows span the full width of the "
            "cabin, flooding the space with natural light and wildlife views. "
            "A plush queen bed, premium linen, and warm ambient lighting make "
            "this the ideal sanctuary for couples and solo adventurers seeking "
            "comfort without compromise."
        ),
        "features": [
            "Full-span panoramic river windows",
            "Queen-size bed with premium bedding",
            "Ambient mood lighting system",
            "En-suite bathroom with shower",
            "Built-in wardrobe and vanity",
            "In-cabin breakfast service available",
        ],
        "amenities": [
            {"label": "Size", "value": "26 m²"},
            {"label": "Bed type", "value": "Queen"},
            {"label": "Bathroom", "value": "En-suite"},
            {"label": "View", "value": "Panoramic river"},
            {"label": "Internet", "value": "Starlink Wi-Fi"},
            {"label": "Climate", "value": "Inverter AC"},
            {"label": "Mini bar", "value": "Available"},
        ],
        "highlights": [
            {
                "title": "Panoramic Windows",
                "desc": "Watch kingfishers, spotted deer, and the occasional tiger from the comfort of your bed.",
            },
            {
                "title": "Mood Lighting",
                "desc": "Adjustable warm ambient lighting to set the perfect atmosphere at any hour.",
            },
            {
                "title": "In-Cabin Dining",
                "desc": "Order from our chef's menu and enjoy breakfast or late-night bites in your own space.",
            },
        ],
    },
    {
        "slug": "family-suite",
        "room_type_name": "4-Person Room",
        "name": "Family Suite",
        "tagline": "Two bedrooms, a private lounge, and a double balcony for the whole family.",
        "size_label": "44 m²",
        "sort_order": 3,
        "description": (
            "The Family Suite is MV Alaska's grandest cabin — a full "
            "two-bedroom sanctuary with a spacious lounge, double balcony, and "
            "a dedicated butler. Designed for families and groups who want to "
            "share the Sundarbans adventure without sacrificing privacy or "
            "luxury, it offers the most generous living space on the vessel."
        ),
        "features": [
            "Two separate bedrooms",
            "Private lounge and living area",
            "Double balcony with river view",
            "Dedicated butler service",
            "Two full en-suite bathrooms",
            "Extra-capacity mini bar & refreshments",
        ],
        "amenities": [
            {"label": "Size", "value": "44 m²"},
            {"label": "Bedrooms", "value": "2 (King + Twin)"},
            {"label": "Bathrooms", "value": "2 en-suite"},
            {"label": "View", "value": "Double balcony"},
            {"label": "Internet", "value": "Starlink Wi-Fi"},
            {"label": "Climate", "value": "Dual-zone AC"},
            {"label": "Butler", "value": "Dedicated"},
        ],
        "highlights": [
            {
                "title": "Double Balcony",
                "desc": "Two separate outdoor terraces let every guest enjoy unobstructed views of the delta.",
            },
            {
                "title": "Private Lounge",
                "desc": "A furnished sitting room for the family to gather, relax, and plan the day's adventures.",
            },
            {
                "title": "Dedicated Butler",
                "desc": "A personal butler handles everything — from packing excursion bags to arranging private dinners.",
            },
        ],
    },
]


def seed_cabins(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    RoomType = apps.get_model("ships", "RoomType")
    Cabin = apps.get_model("ships", "Cabin")

    ship, _ = Ship.objects.get_or_create(name="MV Alaska")

    for entry in CABINS:
        room_type = RoomType.objects.filter(name=entry["room_type_name"]).first()
        Cabin.objects.update_or_create(
            slug=entry["slug"],
            defaults={
                "ship": ship,
                "room_type": room_type,
                "name": entry["name"],
                "tagline": entry["tagline"],
                "description": entry["description"],
                "size_label": entry["size_label"],
                "features": entry["features"],
                "amenities": entry["amenities"],
                "highlights": entry["highlights"],
                "is_active": True,
                "sort_order": entry["sort_order"],
            },
        )


def unseed_cabins(apps, schema_editor):
    Cabin = apps.get_model("ships", "Cabin")
    Cabin.objects.filter(slug__in=[entry["slug"] for entry in CABINS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ships", "0009_cabin_cabinimage"),
    ]

    operations = [
        migrations.RunPython(seed_cabins, unseed_cabins),
    ]
