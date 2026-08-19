"""Create (or update) a superuser from environment variables, idempotently.

Used on hosts without an interactive shell (e.g. Render free tier, where
`createsuperuser` can't be run). Reads DJANGO_SUPERUSER_USERNAME /
DJANGO_SUPERUSER_PASSWORD (and optional DJANGO_SUPERUSER_EMAIL) and is safe to
run on every deploy: if the user already exists it only resets the password,
never duplicates. A no-op when the username/password vars are unset, so it
does nothing once the temporary demo credentials are removed.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Idempotently create/update a superuser from environment variables."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")

        if not username or not password:
            self.stdout.write(
                "ensure_superuser: DJANGO_SUPERUSER_USERNAME/PASSWORD not set; "
                "skipping."
            )
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email},
        )
        user.email = email or user.email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(f"ensure_superuser: {verb} superuser '{username}'.")
        )
