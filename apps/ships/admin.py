from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Cabin,
    CabinImage,
    FoodMenuItem,
    GalleryImage,
    Room,
    RoomImage,
    RoomType,
    Ship,
)


class RoomInline(admin.TabularInline):
    model = Room
    extra = 0


class RoomImageInline(admin.TabularInline):
    model = RoomImage
    extra = 1
    fields = ("preview", "image", "caption", "sort_order")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, room_image):
        if not room_image.image:
            return "—"
        return format_html(
            '<img src="{}" style="max-height:80px;max-width:120px;'
            'object-fit:cover;border-radius:4px;" alt="">',
            room_image.image.url,
        )


@admin.register(Ship)
class ShipAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "total_rooms", "authority_phones")
    list_filter = ("status",)
    fields = ("name", "status", "layout_image", "authority_phones")
    inlines = [RoomInline]


@admin.register(RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "max_adults", "max_kids", "base_price")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("room_number", "floor_number", "ship", "room_type", "image_count")
    list_filter = ("ship", "room_type", "floor_number")
    search_fields = ("room_number",)
    inlines = [RoomImageInline]

    @admin.display(description="Images")
    def image_count(self, room):
        return room.images.count()


class CabinImageInline(admin.TabularInline):
    model = CabinImage
    extra = 1
    fields = ("preview", "image", "caption", "is_main", "sort_order")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, cabin_image):
        if not cabin_image.image:
            return "—"
        return format_html(
            '<img src="{}" style="max-height:80px;max-width:120px;'
            'object-fit:cover;border-radius:4px;" alt="">',
            cabin_image.image.url,
        )


@admin.register(Cabin)
class CabinAdmin(admin.ModelAdmin):
    list_display = ("name", "ship", "room_type", "is_active", "sort_order")
    list_filter = ("ship", "is_active")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [CabinImageInline]


@admin.register(GalleryImage)
class GalleryImageAdmin(admin.ModelAdmin):
    list_display = ("preview", "caption", "ship", "is_active", "sort_order")
    list_filter = ("ship", "is_active")
    list_editable = ("is_active", "sort_order")
    search_fields = ("caption",)

    @admin.display(description="Preview")
    def preview(self, gallery_image):
        if not gallery_image.image:
            return "—"
        return format_html(
            '<img src="{}" style="max-height:80px;max-width:120px;'
            'object-fit:cover;border-radius:4px;" alt="">',
            gallery_image.image.url,
        )


@admin.register(FoodMenuItem)
class FoodMenuItemAdmin(admin.ModelAdmin):
    list_display = ("name", "ship", "day", "meal_type", "order", "is_active")
    list_filter = ("ship", "day", "meal_type", "is_active")
    list_editable = ("order", "is_active")
    search_fields = ("name",)
