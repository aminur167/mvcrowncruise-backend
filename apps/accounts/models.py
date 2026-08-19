from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom user model.

    No extra fields yet — exists so AUTH_USER_MODEL is swappable from day one
    (adding role/permission fields later won't require rebuilding auth tables).
    """
