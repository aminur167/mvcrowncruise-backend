"""
Django settings for the MV Alaska Ship Package Booking System.

All secrets and environment-specific values come from `.env` via django-environ.
See `.env.example` for the required variables.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import dj_database_url
import environ
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

# Local-dev defaults so the project boots without a full .env; every deployed
# environment sets these explicitly (see the Render env-var list in DEPLOY.md).
SECRET_KEY = env("SECRET_KEY", default="dev-insecure-secret-key-change-me")

DEBUG = env("DEBUG")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Fail-safe: never boot a deployed environment with DEBUG on. DEBUG=True leaks
# tracebacks with SECRET_KEY/DB DSN/settings on any 500, and turns off every
# hardening flag below. Railway sets RAILWAY_ENVIRONMENT on every deploy; if we
# see that marker with DEBUG still on, refuse to start rather than silently
# serve insecure. Local dev has no such marker, so DEBUG=True stays fine there.
if DEBUG and env("RAILWAY_ENVIRONMENT", default=""):
    raise ImproperlyConfigured(
        "DEBUG must be False in a deployed environment (RAILWAY_ENVIRONMENT is "
        "set). Set DEBUG=False on the Railway service before deploying."
    )


# Production transport/cookie hardening. Only active when DEBUG is off, so local
# dev (DEBUG=True, plain HTTP) is unaffected. Railway terminates TLS at its edge
# and forwards X-Forwarded-Proto, so SECURE_PROXY_SSL_HEADER lets Django see the
# request as HTTPS and enforce the redirect/secure-cookie flags correctly.
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # ExclusionConstraint (package overlap guard)
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "anymail",
    # Media-only Cloudinary usage, so listed after staticfiles (per its docs;
    # before staticfiles would hijack collectstatic, which stays on WhiteNoise).
    "cloudinary_storage",
    "cloudinary",
    # Local apps
    "apps.accounts",
    "apps.ships",
    "apps.packages",
    "apps.bookings",
    "apps.refunds",
    "apps.staff",
    "apps.contact",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files (incl. Django admin assets) in production.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
#
# Supabase Transaction Pooler (port 6543): connections are handed out per
# transaction from a shared pool, so persistent connections and server-side
# cursors / prepared statements are NOT supported. Hence conn_max_age=0 (open
# and close per request) and DISABLE_SERVER_SIDE_CURSORS=True (Django uses
# client-side cursors, avoiding the "prepared statement already exists" errors
# the pooler otherwise raises).

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        conn_max_age=0,
    ),
}

DISABLE_SERVER_SIDE_CURSORS = True


# Custom user model — must be set before the first migration ever runs.
AUTH_USER_MODEL = "accounts.User"


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Asia/Dhaka"

USE_I18N = True

USE_TZ = True


# Static & media files

STATIC_URL = "static/"
# collectstatic gathers everything here; WhiteNoise serves it in production.
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# Object storage — public imagery on Cloudinary, invoice PDFs on Backblaze B2.
#
# Cloud hosts have ephemeral disks: anything under MEDIA_ROOT (package hero
# images, ship layouts, room photos, invoice PDFs) is wiped on every
# redeploy/restart, so production media must live off-host. The split:
#
# - PUBLIC imagery (hero/layout/room photos) -> Cloudinary when CLOUDINARY_URL
#   is set: permanent CDN URLs (browser caching works) + on-the-fly
#   resize/format transformations. Falls back to B2's media bucket (presigned
#   URLs) if only B2 is configured, else the local filesystem.
# - PRIVATE files (invoice PDFs, customer PII) -> the B2 invoice bucket, short
#   presigned TTL; the customer-facing download is additionally gated by the
#   invoice's own capability token and streams through the app. Never
#   Cloudinary: its free plan does not deliver PDFs, and these must not sit
#   behind permanent public URLs anyway.
#
# Tests always use the filesystem: they create real files (invoice PDFs) and
# must never depend on — or write into — a live bucket/CDN.

TESTING = "test" in sys.argv

CLOUDINARY_URL = env("CLOUDINARY_URL", default="")
USE_CLOUDINARY = not TESTING and bool(CLOUDINARY_URL)
if USE_CLOUDINARY:
    # The cloudinary SDK configures itself from the process environment, and
    # django-environ reads .env into its own store — bridge the two.
    os.environ["CLOUDINARY_URL"] = CLOUDINARY_URL

B2_S3_ENDPOINT_URL = env("B2_S3_ENDPOINT_URL", default="")
B2_ACCESS_KEY_ID = env("B2_ACCESS_KEY_ID", default="")
B2_SECRET_ACCESS_KEY = env("B2_SECRET_ACCESS_KEY", default="")
B2_MEDIA_BUCKET = env("B2_MEDIA_BUCKET", default="")
B2_INVOICE_BUCKET = env("B2_INVOICE_BUCKET", default="")

if B2_S3_ENDPOINT_URL and not B2_S3_ENDPOINT_URL.startswith("http"):
    B2_S3_ENDPOINT_URL = "https://" + B2_S3_ENDPOINT_URL
# The region is embedded in the endpoint host: s3.<region>.backblazeb2.com
B2_REGION = env(
    "B2_REGION",
    default=(
        B2_S3_ENDPOINT_URL.split("//")[-1].split(".")[1]
        if B2_S3_ENDPOINT_URL
        else ""
    ),
)

USE_B2 = not TESTING and all(
    [
        B2_S3_ENDPOINT_URL,
        B2_ACCESS_KEY_ID,
        B2_SECRET_ACCESS_KEY,
        B2_MEDIA_BUCKET,
        B2_INVOICE_BUCKET,
    ]
)

if USE_B2:
    # boto3 >= 1.36 attaches CRC checksums to every request by default, which
    # some S3-compatible providers reject. "when_required" restores the
    # interoperable behaviour (and remains correct against real AWS).
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")


def _b2_storage(bucket_name, url_ttl_seconds):
    return {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "endpoint_url": B2_S3_ENDPOINT_URL,
            "access_key": B2_ACCESS_KEY_ID,
            "secret_key": B2_SECRET_ACCESS_KEY,
            "bucket_name": bucket_name,
            "region_name": B2_REGION,
            "default_acl": None,  # B2 buckets are private; no per-object ACLs
            "querystring_auth": True,  # every URL is presigned
            "querystring_expire": url_ttl_seconds,
            "file_overwrite": False,  # never silently clobber an object
            "signature_version": "s3v4",
        },
    }


_FILESYSTEM = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

if USE_CLOUDINARY:
    _PUBLIC_MEDIA = {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"}
elif USE_B2:
    _PUBLIC_MEDIA = _b2_storage(B2_MEDIA_BUCKET, 24 * 3600)
else:
    _PUBLIC_MEDIA = _FILESYSTEM

# WhiteNoise: compress + hash static filenames for far-future caching.
STORAGES = {
    "default": _PUBLIC_MEDIA,
    # Invoice.pdf_file resolves this alias at runtime (see
    # apps.bookings.models.select_invoice_storage).
    "invoices": _b2_storage(B2_INVOICE_BUCKET, 600) if USE_B2 else _FILESYSTEM,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Default primary key field type

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# CORS — frontend origins (Vite dev server, later Vercel) come from .env.

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])


# DRF — the public API is read-only and unauthenticated; write endpoints
# (Phase 3+) will declare their own stricter permissions.

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    # Defensive default so no list endpoint can ever return an unbounded set by
    # omission (QA phase8b F3). Staff viewsets set their own StaffPagination;
    # endpoints that must return a whole set (the package room map) opt out
    # explicitly with pagination_class = None.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/min",
        "booking": "10/min",
        # Live price previews (quote) — fired per guest-count change in the
        # wizard, so far looser than actual booking creation.
        "quote": "60/min",
        # Read-only availability/calendar/package browsing. These are cheap,
        # cacheable-shaped GETs and are the two moments a 429 is most damaging
        # (discovery, and the post-payment status poll) — so they get their own
        # generous bucket instead of sharing the 100/min anon budget. That
        # budget is keyed on the real client IP, so several customers behind one
        # NAT/carrier IP browsing availability could otherwise collectively trip
        # it mid-booking (QA phase8b F1). Abuse of a read-only endpoint has no
        # upside to prevent the way hammering `create` does.
        "read": "600/min",
        # The frontend polls the booking-status GET every 2s up to ~6 times
        # after payment (useBooking pollWhilePending). Give it headroom so a
        # legitimate poll is never throttled, even with the wizard traffic that
        # precedes it on the same IP.
        "status": "120/min",
        # Staff login + token refresh: tight bucket to blunt credential
        # stuffing / password spraying against the admin dashboard. Keyed on
        # the real client IP (NUM_PROXIES set below), not a spoofable header.
        "login": "5/min",
        # Public contact-form submissions: anonymous and land in the staff
        # inbox, so keep the per-IP rate low enough that the form can't be used
        # to flood it, while still comfortable for a genuine sender.
        "contact": "5/min",
        # "Find my booking": the only public endpoint where the caller supplies
        # a booking code we did not hand them, i.e. the one place a code could
        # be searched for. 64 bits of entropy already makes that hopeless, but
        # the second factor is four digits — this bucket is what keeps THAT
        # from being guessable against a code someone genuinely holds.
        "lookup": "10/min",
        # Cancellation submissions. Rare by nature (a customer cancels a
        # holiday once), each one emails staff, and each one is a write against
        # money — so the budget is deliberately smaller than booking creation.
        "cancellation": "5/min",
    },
    # Trusted proxy hop count for throttling. Without it DRF keys throttle
    # buckets on the raw client-supplied X-Forwarded-For header, which lets
    # anyone bypass every rate limit (new header per request) or poison
    # someone else's bucket. Match the real proxy depth of the deployment
    # (Railway: 1).
    "NUM_PROXIES": env.int("DRF_NUM_PROXIES", default=1),
    "EXCEPTION_HANDLER": "config.exceptions.exception_handler",
}


# Staff dashboard auth — JWT (short-lived access + rotating refresh tokens).

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}


# SSLCommerz payment gateway — credentials only ever come from .env.

SSLCOMMERZ_STORE_ID = env("SSLCOMMERZ_STORE_ID", default="")
SSLCOMMERZ_STORE_PASSWORD = env("SSLCOMMERZ_STORE_PASSWORD", default="")
SSLCOMMERZ_IS_SANDBOX = env.bool("SSLCOMMERZ_IS_SANDBOX", default=True)

_SSLCOMMERZ_BASE = (
    "https://sandbox.sslcommerz.com"
    if SSLCOMMERZ_IS_SANDBOX
    else "https://securepay.sslcommerz.com"
)
SSLCOMMERZ_SESSION_URL = f"{_SSLCOMMERZ_BASE}/gwprocess/v4/api.php"
SSLCOMMERZ_VALIDATION_URL = f"{_SSLCOMMERZ_BASE}/validator/api/validationserverAPI.php"
# Transaction Query API — look up a session by OUR tran_id (no val_id needed).
# Used by reconcile_pending_payments and the fail/cancel redirect handlers.
SSLCOMMERZ_TXN_QUERY_URL = (
    f"{_SSLCOMMERZ_BASE}/validator/api/merchantTransIDvalidationAPI.php"
)

BACKEND_URL = env("BACKEND_URL", default="http://localhost:8000")
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")

# Unpaid PENDING bookings are auto-cancelled after this hold window.
BOOKING_HOLD_MINUTES = env.int("BOOKING_HOLD_MINUTES", default=30)

# Authority helpline numbers printed on the guide report & customer invoice
# (comma-separated). Change these without touching code.
AUTHORITY_PHONES = env(
    "AUTHORITY_PHONES", default="01712-823482,01831-694307,01342-919795"
)

# Where public /contact form submissions are emailed. This is the system-wide
# default; a ship may override it from the staff dashboard (Ship.contact_notify_email).
CONTACT_NOTIFY_EMAIL = env("CONTACT_NOTIFY_EMAIL", default="mahmudulabin@gmail.com")

# The gateway session lifetime: once a PENDING payment is older than this
# AND SSLCommerz's Transaction Query API reports no payment attempt on it,
# reconcile_pending_payments closes it as FAILED. (Room holds themselves are
# never released on a timer while a PENDING payment exists — only after the
# gateway has confirmed the session is dead.)
# Must be >= the gateway's own session lifetime.
PAYMENT_SESSION_MINUTES = env.int("PAYMENT_SESSION_MINUTES", default=30)

# How many days before the balance deadline the one-off reminder email goes
# out (enforce_due_deadlines). The deadline itself is per-package data:
# Package.balance_due_days_before_start.
BALANCE_DUE_REMINDER_DAYS = env.int("BALANCE_DUE_REMINDER_DAYS", default=2)

# Consecutive gateway-query failures on one PENDING payment before
# reconcile_pending_payments escalates it for manual review. A payment the
# gateway won't answer for holds its room out of inventory, so it must reach a
# human rather than spin forever.
PAYMENT_MAX_RECONCILE_ATTEMPTS = env.int("PAYMENT_MAX_RECONCILE_ATTEMPTS", default=5)

# An escalated payment is not abandoned — it is retried on this slow back-off.
# Gateway outages end, and a payment nobody asks about again keeps its cabin
# out of inventory forever; a recovered gateway auto-resolves the backlog and
# only genuinely undecidable payments wait for the human already notified.
PAYMENT_ESCALATED_RETRY_MINUTES = env.int(
    "PAYMENT_ESCALATED_RETRY_MINUTES", default=60
)


# Email — provider-agnostic, everything from .env.
# Dev default prints emails to the console; production sets a real backend.
#
# Prefer an HTTP-API backend (Resend via Anymail) in the cloud: hosts like
# Render block outbound SMTP ports (25/465/587), so an SMTP backend hangs until
# the gunicorn worker times out and is SIGKILLed mid-payment. Resend goes over
# HTTPS, so it is not blocked. To use it, set on the host:
#   EMAIL_BACKEND=anymail.backends.resend.EmailBackend
#   RESEND_API_KEY=<your key>
#   DEFAULT_FROM_EMAIL=MV Alaska <onboarding@resend.dev>   (or your verified domain)

EMAIL_BACKEND = env(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
# Belt-and-suspenders: cap any SMTP backend so a blocked port can never hold a
# worker past gunicorn's timeout again. Ignored by the HTTP (Resend) backend.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="MV Alaska <noreply@localhost>")

# Anymail (Resend) — API key only from env. Harmless when the console/SMTP
# backend is in use.
ANYMAIL = {
    "RESEND_API_KEY": env("RESEND_API_KEY", default=""),
}
